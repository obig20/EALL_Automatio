"""Crash-conscious JSONL storage for inquiries and an append-only event log."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from .utils import now_iso

_LOCK = threading.RLock()
INQUIRY_STATES = {
    "received",
    "matched",
    "drafting",
    "awaiting_review",
    "escalated",
    "approved",
    "rejected",
    "sending",
    "sent",
    "send_failed",
    "draft_failed",
}


class StorageError(RuntimeError):
    """Raised when persisted state cannot be read or safely updated."""


class DuplicateInquiryError(StorageError):
    """A Telegram message that is already persisted was delivered again.

    The carried inquiry is the durable record that already exists, so the
    caller can safely treat the redelivery as already processed.
    """

    def __init__(self, inquiry: dict[str, Any]):
        self.inquiry = inquiry
        super().__init__(
            f"Inquiry already exists for Telegram message: "
            f"{inquiry.get('inquiry_id')}"
        )


def _dedup_key(
    telegram_chat_id: str | int | None,
    telegram_user_id: str | int | None,
    telegram_message_id: str | int | None,
) -> tuple[str, str] | None:
    """Stable inbound-message identity, or None when the record is anonymous.

    Telegram message IDs are unique per chat, so the chat (falling back to the
    user for legacy records) plus the message ID identifies one inbound message.
    """
    if telegram_message_id is None:
        return None
    sender = telegram_chat_id if telegram_chat_id is not None else telegram_user_id
    if sender is None:
        return None
    return (str(sender), str(telegram_message_id))


class JSONLStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.inquiries_path = self.data_dir / "inquiries.jsonl"
        self.events_path = self.data_dir / "events.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.inquiries_path, self.events_path):
            path.touch(exist_ok=True)

    def _read_records(self, path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise StorageError(
                            f"Malformed JSON in {path.name} at line {line_number}"
                        ) from exc
                    if not isinstance(record, dict):
                        raise StorageError(
                            f"Expected an object in {path.name} at line {line_number}"
                        )
                    records.append(record)
        except OSError as exc:
            raise StorageError(f"Unable to read {path.name}: {exc}") from exc
        return records

    def _replace_records(self, path: Path, records: list[dict[str, Any]]) -> None:
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_name = stream.name
                for record in records:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except OSError as exc:
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            raise StorageError(f"Unable to update {path.name}: {exc}") from exc

    def list_inquiries(self) -> list[dict[str, Any]]:
        with _LOCK:
            return self._read_records(self.inquiries_path)

    def get_inquiry(self, inquiry_id: str) -> dict[str, Any] | None:
        with _LOCK:
            return next(
                (
                    inquiry
                    for inquiry in self._read_records(self.inquiries_path)
                    if inquiry.get("inquiry_id") == inquiry_id
                ),
                None,
            )

    def create_inquiry(
        self,
        *,
        telegram_user_id: str | int | None,
        telegram_message_id: str | int | None,
        message: str,
        received_at: str | None = None,
        inquiry_id: str | None = None,
        telegram_chat_id: str | int | None = None,
    ) -> dict[str, Any]:
        with _LOCK:
            inquiries = self._read_records(self.inquiries_path)
            # Inbound deduplication: the same Telegram chat + message ID must
            # never create a second inquiry, a second draft, or a second
            # founder notification. The check and the append happen under the
            # same lock, and the original record was already fsynced, so a
            # duplicate is only reported once its state is fully durable.
            key = _dedup_key(
                telegram_chat_id, telegram_user_id, telegram_message_id
            )
            if key is not None:
                for existing in inquiries:
                    if _dedup_key(
                        existing.get("telegram_chat_id"),
                        existing.get("telegram_user_id"),
                        existing.get("telegram_message_id"),
                    ) == key:
                        raise DuplicateInquiryError(existing)
            if inquiry_id is None:
                sequence = max(
                    (
                        int(str(item.get("inquiry_id", "inq_000000")).removeprefix("inq_"))
                        for item in inquiries
                        if str(item.get("inquiry_id", "")).startswith("inq_")
                        and str(item.get("inquiry_id", ""))[4:].isdigit()
                    ),
                    default=0,
                )
                inquiry_id = f"inq_{sequence + 1:06d}"
            if any(item.get("inquiry_id") == inquiry_id for item in inquiries):
                raise StorageError(f"Inquiry already exists: {inquiry_id}")
            timestamp = received_at or now_iso()
            inquiry = {
                "inquiry_id": inquiry_id,
                "telegram_user_id": (
                    str(telegram_user_id) if telegram_user_id is not None else None
                ),
                "telegram_chat_id": (
                    str(telegram_chat_id) if telegram_chat_id is not None else None
                ),
                "telegram_message_id": (
                    str(telegram_message_id) if telegram_message_id is not None else None
                ),
                "received_at": timestamp,
                "message": message,
                "matched_properties": [],
                "property_context": [],
                "draft": None,
                "requires_escalation": False,
                "status": "received",
                "approved_at": None,
                "sent_at": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "send_attempts": 0,
            }
            inquiries.append(inquiry)
            self._replace_records(self.inquiries_path, inquiries)
            return inquiry.copy()

    def update_inquiry(self, inquiry_id: str, **changes: Any) -> dict[str, Any]:
        with _LOCK:
            inquiries = self._read_records(self.inquiries_path)
            for index, inquiry in enumerate(inquiries):
                if inquiry.get("inquiry_id") != inquiry_id:
                    continue
                if "inquiry_id" in changes and changes["inquiry_id"] != inquiry_id:
                    raise StorageError("An inquiry ID cannot be changed")
                new_status = changes.get("status", inquiry.get("status"))
                if new_status not in INQUIRY_STATES:
                    raise StorageError(f"Invalid inquiry status: {new_status}")
                inquiry.update(changes)
                inquiry["updated_at"] = now_iso()
                inquiries[index] = inquiry
                self._replace_records(self.inquiries_path, inquiries)
                return inquiry.copy()
            raise StorageError(f"Inquiry not found: {inquiry_id}")

    def get_pending_inquiries(self) -> list[dict[str, Any]]:
        pending_states = {
            "received",
            "matched",
            "drafting",
            "awaiting_review",
            "escalated",
            "approved",
            "sending",
            "send_failed",
            "draft_failed",
        }
        return [
            inquiry
            for inquiry in self.list_inquiries()
            if inquiry.get("status") in pending_states
        ]

    def append_event(
        self,
        event: str,
        inquiry_id: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        record = {
            "timestamp": now_iso(),
            "event": event,
            "inquiry_id": inquiry_id,
            **details,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            try:
                with self.events_path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise StorageError(f"Unable to append event: {exc}") from exc
        return record

    def list_events(self) -> list[dict[str, Any]]:
        with _LOCK:
            return self._read_records(self.events_path)


def create_inquiry(data_dir: str | Path, **kwargs: Any) -> dict[str, Any]:
    return JSONLStore(data_dir).create_inquiry(**kwargs)


def get_inquiry(data_dir: str | Path, inquiry_id: str) -> dict[str, Any] | None:
    return JSONLStore(data_dir).get_inquiry(inquiry_id)


def update_inquiry(
    data_dir: str | Path, inquiry_id: str, **changes: Any
) -> dict[str, Any]:
    return JSONLStore(data_dir).update_inquiry(inquiry_id, **changes)


def append_event(
    data_dir: str | Path, event: str, inquiry_id: str | None = None, **details: Any
) -> dict[str, Any]:
    return JSONLStore(data_dir).append_event(event, inquiry_id, **details)