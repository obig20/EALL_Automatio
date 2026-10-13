"""Deterministic property lookup; this data is the only property source of truth."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .catalog import normalize_property_record
from .config import get_settings

WORD_ALIASES = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}

# Property types are mutually exclusive. A villa request must never be
# satisfied by an apartment record, and vice versa. When a buyer names a
# type explicitly, only records of a compatible type may be considered.
PROPERTY_TYPE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("apartment", "flat", "condo", "condominium", "unit"),
    ("villa", "house", "home", "bungalow", "townhouse", "mansion", "cottage", "residence"),
    ("land", "plot", "parcel", "lot", "ground"),
    ("office", "commercial", "retail", "shop", "store", "warehouse"),
    ("hotel", "lodging", "inn", "aparthotel"),
)

# Only these nouns are specific enough to act as a hard constraint.
# Generic words such as "house" or "home" are excluded: buyers use them
# loosely to mean "property", so "the house in Bole" must still reach the
# Bole apartments.
_HARD_TYPE_TOKENS: frozenset[str] = frozenset(
    {
        "apartment", "flat", "condo", "condominium",
        "villa", "bungalow", "townhouse", "mansion", "cottage", "residence",
        "land", "plot", "parcel", "lot", "ground",
        "office", "commercial", "retail", "shop", "store", "warehouse",
        "hotel", "lodging", "inn", "aparthotel",
    }
)


def _type_group(property_type: str) -> str | None:
    """Return the group label for a property type, or None when unknown."""
    normalized = _normalize(property_type)
    if not normalized:
        return None
    for index, group in enumerate(PROPERTY_TYPE_GROUPS):
        if normalized in group:
            return f"group{index}"
    return None


def _type_tokens(property_type: str) -> set[str]:
    """All normalized type tokens for a record, including synonyms.

    A record typed "Apartment" also exposes "flat" and "condo" so that a
    buyer asking for a flat can still reach it.
    """
    normalized = _normalize(property_type)
    if not normalized:
        return set()
    tokens = {normalized}
    for group in PROPERTY_TYPE_GROUPS:
        if normalized in group:
            tokens.update(group)
    return tokens


def _catalog_path() -> Path:
    """Resolve the active property catalog path.

    PROPERTIES_FILE selects an explicit catalog. When it points at a
    directory, the file inside it named ``properties.json`` is used, so an
    imported catalog directory can be supplied without changing callers.
    The demo catalog is the default and is never overwritten by import.
    """
    path = get_settings(validate=False).properties_file
    if path.is_dir():
        return path / "properties.json"
    return path


@lru_cache(maxsize=8)
def _load_properties(path_text: str, mtime_ns: int, size: int) -> dict[str, dict[str, Any]]:
    del mtime_ns, size
    path = Path(path_text)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to load property data from {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Property data must be an object: {path}")
    for property_id, property_data in raw.items():
        if property_id.startswith("_"):
            continue
        if not isinstance(property_data, dict) or not property_data.get("name"):
            raise ValueError(f"Invalid property record: {property_id}")
    records = {key: value for key, value in raw.items() if not key.startswith("_")}
    # Normalize every record so consumers see one consistent schema
    # regardless of whether the record came from the demo catalog or an
    # imported listing. The source record is never mutated.
    return {pid: normalize_property_record(data) for pid, data in records.items()}


def get_all_properties() -> dict[str, dict[str, Any]]:
    path = _catalog_path()
    stat = path.stat()
    records = _load_properties(str(path), stat.st_mtime_ns, stat.st_size)
    return {key: value.copy() for key, value in records.items()}


def get_property(property_id: str) -> dict[str, Any] | None:
    property_data = get_all_properties().get(property_id)
    return property_data.copy() if property_data else None


def search_properties(query: str) -> list[dict[str, Any]]:
    query_key = _normalize(query)
    if not query_key:
        return []
    return [
        {"property_id": property_id, **property_data}
        for property_id, property_data in get_all_properties().items()
        if query_key in _normalize(
            " ".join(
                [
                    property_id,
                    str(property_data.get("name", "")),
                    str(property_data.get("location", "")),
                ]
            )
        )
    ]


def get_property_context(property_id: str) -> dict[str, Any] | None:
    return get_property(property_id)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _bedroom_match(message: str, bedrooms: int) -> bool:
    text = _normalize(message)
    numeric = re.search(rf"\b{bedrooms}\s*(?:bed(?:room)?s?|br)\b", text)
    word = next(
        (name for name, value in WORD_ALIASES.items() if value == bedrooms), None
    )
    spelled = bool(word and re.search(rf"\b{word}\s+bed(?:room)?s?\b", text))
    return bool(numeric or spelled)


def _buyer_type_group(message: str) -> str | None:
    """Return the property-type group the buyer named, if any.

    Only unambiguous property-type nouns constrain the catalog. Generic
    words such as "house" or "home" are excluded: buyers use them to mean
    "property" loosely, and a Bole apartment request written as "the house
    in Bole" must still reach the Bole apartments. "Villa", "apartment",
    "flat", "land", "plot", "office", and "hotel" are specific enough to
    filter incompatible records out.
    """
    tokens = set(_normalize(message).split())
    for index, group in enumerate(PROPERTY_TYPE_GROUPS):
        constraining = {
            token
            for token in group
            if token in _HARD_TYPE_TOKENS and token in tokens
        }
        if constraining:
            return f"group{index}"
    return None


def _buyer_bedrooms(message: str) -> int | None:
    """Return the bedroom count the buyer named, if any."""
    match = re.search(r"\b(\d+)\s*(?:bed(?:room)?s?|br)\b", _normalize(message))
    if match:
        return int(match.group(1))
    for name, value in WORD_ALIASES.items():
        if re.search(rf"\b{name}\s+bed(?:room)?s?\b", _normalize(message)):
            return value
    return None


def _score_property(message: str, property_id: str, property_data: dict[str, Any]) -> int:
    normalized = _normalize(message)
    tokens = set(normalized.split())
    score = 0

    identifiers = [property_id.replace("_", " "), property_data.get("name", "")]
    if any(_normalize(identifier) in normalized for identifier in identifiers if identifier):
        score += 10

    location_terms = [
        token
        for token in _normalize(str(property_data.get("location", ""))).split()
        if len(token) >= 3 and token not in {"near", "road"}
    ]
    if location_terms and all(token in tokens for token in location_terms):
        score += 7
    elif location_terms and any(token in tokens for token in location_terms):
        score += 3

    bedrooms = property_data.get("bedrooms")
    if isinstance(bedrooms, int) and _bedroom_match(message, bedrooms):
        score += 5

    property_type = _normalize(str(property_data.get("property_type", "")))
    if property_type and property_type in tokens:
        score += 2
    return score


def _hard_filter(
    message: str, property_id: str, property_data: dict[str, Any]
) -> bool:
    """Reject records that contradict an explicit buyer constraint.

    Explicit property-type and bedroom constraints are hard constraints:
    a villa request cannot be satisfied by an apartment record, and a
    three-bedroom request cannot be satisfied by a two-bedroom record.
    """
    buyer_type_group = _buyer_type_group(message)
    if buyer_type_group is not None:
        record_type_group = _type_group(str(property_data.get("property_type", "")))
        if record_type_group is None or record_type_group != buyer_type_group:
            return False

    buyer_bedrooms = _buyer_bedrooms(message)
    if buyer_bedrooms is not None:
        record_bedrooms = property_data.get("bedrooms")
        if not isinstance(record_bedrooms, int) or record_bedrooms != buyer_bedrooms:
            return False
    return True


def find_relevant_properties(message: str) -> list[str]:
    """Return IDs sorted by deterministic relevance, without semantic guessing.

    Explicit property-type and bedroom constraints are hard constraints, so
    incompatible records are rejected even when location or bedroom count
    alone would have matched. All records that survive the hard filter and
    share the highest relevance score are returned, so ambiguous inquiries
    surface multiple candidates instead of silently choosing one property.
    """
    if not message or not message.strip():
        return []
    candidates = [
        (property_id, _score_property(message, property_id, details))
        for property_id, details in get_all_properties().items()
        if _hard_filter(message, property_id, details)
    ]
    ranked = sorted(candidates, key=lambda result: (-result[1], result[0]))
    if not ranked or ranked[0][1] == 0:
        return []
    highest_score = ranked[0][1]
    return [property_id for property_id, score in ranked if score == highest_score]