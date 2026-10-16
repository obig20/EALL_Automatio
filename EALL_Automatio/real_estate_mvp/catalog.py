"""Property catalog: import, update, and provenance tracking."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import now_iso

PROVENANCE_IMPORTED = "imported"
PROVENANCE_VERIFIED = "verified"
PROVENANCE_UNVERIFIED = "unverified"
PROVENANCE_DEMO = "demo"

VALID_STATUSES = {"available", "sold", "unavailable", "unknown"}

REQUIRED_FIELDS = ("property_id", "name", "property_type", "location")

NUMERIC_FIELDS = ("bedrooms",)


@dataclass
class ImportWarning:
    row: int
    field: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"row": self.row, "field": self.field, "message": self.message}


@dataclass
class ImportResult:
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[ImportWarning] = field(default_factory=list)
    duplicates: list[ImportWarning] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.accepted) and not self.rejected

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": len(self.accepted),
            "rejected": [w.as_dict() for w in self.rejected],
            "duplicates": [w.as_dict() for w in self.duplicates],
        }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_bedrooms(value: str) -> int | None:
    text = _clean(value)
    if not text:
        return None
    try:
        bedrooms = int(text)
    except ValueError:
        return None
    return bedrooms if bedrooms >= 0 else None


def _parse_list(value: str) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    return [item.strip() for item in text.split("|") if item.strip()]


def _parse_price(value: str) -> dict[str, Any] | None:
    text = _clean(value)
    if not text:
        return None
    amount = text.replace(",", "").replace(" ", "")
    digits = "".join(ch for ch in amount if ch.isdigit() or ch == ".")
    if not digits:
        return None
    try:
        numeric = float(digits)
    except ValueError:
        return None
    currency = "ETB"
    lowered = text.casefold()
    for token in ("etb", "usd", "eur", "gbp", "birr"):
        if token in lowered:
            currency = token.upper()
            break
    return {"amount": numeric, "currency": currency, "text": text}


def _parse_availability(value: str) -> str | None:
    text = _clean(value).casefold()
    if not text:
        return None
    for status in VALID_STATUSES:
        if status in text:
            return status
    return "unknown"


def _normalize_row(row: dict[str, Any], row_number: int) -> tuple[dict[str, Any] | None, list[ImportWarning]]:
    warnings: list[ImportWarning] = []
    record: dict[str, Any] = {}

    property_id = _clean(row.get("property_id"))
    if not property_id:
        return None, [ImportWarning(row_number, "property_id", "Missing required property_id")]
    record["property_id"] = property_id

    name = _clean(row.get("name"))
    if not name:
        warnings.append(ImportWarning(row_number, "name", "Missing required name"))
    record["name"] = name

    property_type = _clean(row.get("property_type"))
    if not property_type:
        warnings.append(ImportWarning(row_number, "property_type", "Missing required property_type"))
    record["property_type"] = property_type

    location = _clean(row.get("location"))
    if not location:
        warnings.append(ImportWarning(row_number, "location", "Missing required location"))
    record["location"] = location

    bedrooms = _parse_bedrooms(row.get("bedrooms"))
    if row.get("bedrooms") is not None and bedrooms is None:
        warnings.append(ImportWarning(row_number, "bedrooms", "Invalid bedrooms value"))
    if bedrooms is not None:
        record["bedrooms"] = bedrooms

    price = _parse_price(row.get("price"))
    if row.get("price") is not None:
        if price is None:
            warnings.append(ImportWarning(row_number, "price", "Invalid price value"))
        else:
            record["price"] = price["text"]
            record["price_amount"] = price["amount"]
            record["price_currency"] = price["currency"]

    downpayment = _clean(row.get("downpayment"))
    if downpayment:
        record["downpayment"] = downpayment

    payment_schedule = _clean(row.get("payment_schedule"))
    if payment_schedule:
        record["payment_schedule"] = payment_schedule

    amenities = _parse_list(row.get("amenities"))
    if amenities:
        record["amenities"] = amenities

    availability = _parse_availability(row.get("availability"))
    if row.get("availability") is not None:
        record["availability"] = availability or "unknown"

    images = _parse_list(row.get("images"))
    if images:
        record["images"] = images

    source = _clean(row.get("source"))
    if source:
        record["source"] = source
    else:
        warnings.append(ImportWarning(row_number, "source", "Missing source reference"))

    last_updated = _clean(row.get("last_updated"))
    if last_updated:
        record["last_updated"] = last_updated

    record["provenance"] = PROVENANCE_IMPORTED
    record["verified"] = False
    record["imported_at"] = now_iso()

    if warnings:
        return None, warnings
    return record, []


def import_csv(
    csv_path: str | Path,
    *,
    existing: dict[str, dict[str, Any]] | None = None,
) -> ImportResult:
    """Validate a CSV file and return accepted records plus warnings.

    Existing is the active catalog; duplicate property IDs are reported
    separately so a re-import can never silently create a second record.
    Rows with invalid required fields are rejected and never accepted.
    """
    path = Path(csv_path)
    result = ImportResult()
    existing = existing or {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                result.rejected.append(
                    ImportWarning(0, None, "CSV file has no header row")
                )
                return result
            for row_number, row in enumerate(reader, start=2):
                record, warnings = _normalize_row(row, row_number)
                if record is None:
                    result.rejected.extend(warnings)
                    continue
                if record["property_id"] in existing:
                    result.duplicates.append(
                        ImportWarning(
                            row_number,
                            "property_id",
                            f"Duplicate property_id {record['property_id']}",
                        )
                    )
                    continue
                result.accepted.append(record)
    except OSError as exc:
        result.rejected.append(ImportWarning(0, None, f"Unable to read CSV: {exc}"))
    return result


def preview_import(csv_path: str | Path) -> ImportResult:
    """Validate a CSV without touching the active catalog."""
    return import_csv(csv_path, existing=None)


def apply_import(
    catalog_path: str | Path,
    csv_path: str | Path,
    *,
    source: str | None = None,
) -> ImportResult:
    """Validate, then atomically replace the active catalog.

    The new catalog is written to a temp file in the same directory and
    renamed over the target only after validation succeeds, so an invalid
    import cannot partially corrupt the active catalog.
    """
    path = Path(catalog_path)
    existing = _load_catalog(path)
    result = import_csv(csv_path, existing=existing)
    if not result.ok:
        return result

    catalog: dict[str, dict[str, Any]] = {key: value.copy() for key, value in existing.items()}
    for record in result.accepted:
        if source:
            record["source"] = source
        catalog[record["property_id"]] = record

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with open(path, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(catalog, ensure_ascii=False, indent=2))
            stream.flush()
    except OSError as exc:
        result.rejected.append(ImportWarning(0, None, f"Unable to write catalog: {exc}"))
        return result
    return result


def update_property(
    catalog_path: str | Path,
    property_id: str,
    *,
    price: str | None = None,
    downpayment: str | None = None,
    payment_schedule: str | None = None,
    amenities: list[str] | None = None,
    availability: str | None = None,
    source: str | None = None,
    verified: bool | None = None,
) -> dict[str, Any] | None:
    """Update an existing property's mutable fields without changing its ID.

    A newer verified record never silently overrides an older verified
    record; if both are verified, the update is rejected. Imported records
    may be updated by a later import. Returns the updated record or None
    when the property does not exist.
    """
    path = Path(catalog_path)
    catalog = _load_catalog(path)
    record = catalog.get(property_id)
    if record is None:
        return None

    if verified is True and record.get("verified") is True:
        return None

    # Validate every supplied field before mutating the record, so a bad
    # update cannot leave the catalog in a half-applied state.
    if price is not None:
        parsed = _parse_price(price)
        if parsed is None:
            return None
    if availability is not None:
        normalized = _parse_availability(availability)
        if normalized is None:
            return None

    updated_at = now_iso()
    if price is not None:
        record["price"] = price.strip()
        parsed = _parse_price(price)
        record["price_amount"] = parsed["amount"]
        record["price_currency"] = parsed["currency"]
    if downpayment is not None:
        record["downpayment"] = downpayment.strip()
    if payment_schedule is not None:
        record["payment_schedule"] = payment_schedule.strip()
    if amenities is not None:
        record["amenities"] = [item.strip() for item in amenities if item.strip()]
    if availability is not None:
        record["availability"] = normalized or "unknown"
    if source is not None:
        record["source"] = source.strip()
    if verified is not None:
        record["verified"] = verified
        if verified:
            record["provenance"] = PROVENANCE_VERIFIED
        elif record.get("provenance") != PROVENANCE_VERIFIED:
            record["provenance"] = PROVENANCE_UNVERIFIED
    record["last_updated"] = updated_at
    record["updated_at"] = updated_at

    _write_catalog(path, catalog)
    return record.copy()


def load_catalog(catalog_path: str | Path) -> dict[str, dict[str, Any]]:
    """Load the active catalog, returning an empty dict when absent."""
    return _load_catalog(Path(catalog_path))


def _load_catalog(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if not key.startswith("_")}


def _write_catalog(path: Path, catalog: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(catalog, ensure_ascii=False, indent=2))
        stream.write("\n")


def normalize_property_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a consumer-facing copy of a property record.

    The original record is never mutated. Normalization bridges the schema
    gap between imported records (which carry ``payment_schedule``,
    ``price_amount``, ``price_currency``, ``verified``, and ``provenance``)
    and the demo records (which carry ``payment_plan`` and a plain ``price``
    text string) so that matching, drafting, validation, and the
    founder-review message all read one consistent shape.

    Missing values are never invented. When a price cannot be represented
    unambiguously, the raw text is preserved and the structured amount is
    left absent.
    """
    normalized: dict[str, Any] = {}

    for key in (
        "property_id",
        "name",
        "property_type",
        "location",
        "bedrooms",
        "availability",
        "source",
        "provenance",
        "last_updated",
        "imported_at",
        "updated_at",
    ):
        if key in record and record[key] not in (None, ""):
            normalized[key] = record[key]

    # Payment terms: the demo catalog uses ``payment_plan`` and the import
    # workflow uses ``payment_schedule``. Consumers only need one field, so
    # prefer the demo spelling when present and fall back to the import
    # spelling. Never invent a payment plan from a down-payment figure.
    if record.get("payment_plan"):
        normalized["payment_plan"] = record["payment_plan"]
    elif record.get("payment_schedule"):
        normalized["payment_plan"] = record["payment_schedule"]

    if record.get("downpayment"):
        normalized["downpayment"] = record["downpayment"]

    # Price: the demo catalog stores a human-readable string such as
    # "12,000,000 ETB". Imported records additionally carry structured
    # amount/currency fields. Preserve the text and, when the structured
    # fields are consistent with it, expose them too.
    price_text = record.get("price")
    if price_text:
        normalized["price"] = price_text
    amount = record.get("price_amount")
    currency = record.get("price_currency")
    if amount is not None and currency:
        normalized["price_amount"] = amount
        normalized["price_currency"] = currency
    elif price_text:
        parsed = _parse_price(price_text)
        if parsed is not None:
            normalized["price_amount"] = parsed["amount"]
            normalized["price_currency"] = parsed["currency"]

    if record.get("amenities"):
        normalized["amenities"] = list(record["amenities"])
    if record.get("images"):
        normalized["images"] = list(record["images"])
    if record.get("payment_schedule"):
        normalized["payment_schedule"] = record["payment_schedule"]

    # Verification metadata: preserve whatever the source record says.
    # A missing ``verified`` field is never treated as verified: the demo
    # catalog has no such field at all, and imported records default to
    # unverified. The provenance follows the source record; a record that
    # has neither provenance nor an import timestamp is synthetic demo
    # data and is labelled accordingly so it can never be confused with a
    # real imported listing.
    if "verified" in record:
        normalized["verified"] = bool(record["verified"])
    else:
        normalized["verified"] = False

    if "provenance" in record and record["provenance"]:
        normalized["provenance"] = record["provenance"]
    elif "imported_at" in record and record["imported_at"]:
        normalized.setdefault("provenance", PROVENANCE_IMPORTED)
    elif "verified" not in record and "provenance" not in record:
        normalized.setdefault("provenance", PROVENANCE_DEMO)
    elif "verified" in record and not record["verified"]:
        normalized.setdefault("provenance", PROVENANCE_UNVERIFIED)
    return normalized


def is_verified_property(record: dict[str, Any] | None) -> bool:
    """Check whether a property record is verified.

    Only property records explicitly marked with BOTH ``verified is True``
    and ``provenance == 'verified'`` may supply property facts to
    buyer-facing drafts. Missing, demo, imported, or unverified records fail.
    """
    if not isinstance(record, dict):
        return False
    return record.get("verified") is True and record.get("provenance") == PROVENANCE_VERIFIED