"""Deterministic property lookup; this data is the only property source of truth."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import get_settings

WORD_ALIASES = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}


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
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def _properties_file() -> Path:
    return get_settings(validate=False).properties_file


def get_all_properties() -> dict[str, dict[str, Any]]:
    path = _properties_file()
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


def find_relevant_properties(message: str) -> list[str]:
    """Return IDs sorted by deterministic relevance, without semantic guessing."""
    if not message or not message.strip():
        return []
    scores = [
        (property_id, _score_property(message, property_id, details))
        for property_id, details in get_all_properties().items()
    ]
    ranked = sorted(scores, key=lambda result: (-result[1], result[0]))
    if not ranked or ranked[0][1] == 0:
        return []
    highest_score = ranked[0][1]
    return [property_id for property_id, score in ranked if score == highest_score]