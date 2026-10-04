"""Founder-only review, approval, rejection, and explicit manual sending."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from .retry import with_transient_retry_async
from .storage import JSONLStore
from .utils import now_iso

SendFunction = Callable[[int, str], Awaitable[Any] | Any]


def format_review_message(inquiry: dict[str, Any]) -> str:
    status = inquiry["status"]
    escalation = inquiry.get("requires_escalation", False)
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "NEW REAL-ESTATE INQUIRY",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Inquiry ID: {inquiry['inquiry_id']}",
        f"Buyer: Telegram user {inquiry.get('telegram_user_id') or 'unknown'}",
        "",
        "BUYER MESSAGE:",
        inquiry.get("message", ""),
        "",
        "VERIFIED PROPERTY DATA:",
    ]
    contexts = inquiry.get("property_context") or []
    if contexts:
        for property_data in contexts:
            lines.extend(
                [
                    "DEMO DATA — not a current real listing",
                    f"Property: {property_data.get('name', 'Unknown')}",
                    f"Type: {property_data.get('property_type', 'Not documented')}",
                    f"Bedrooms: {property_data.get('bedrooms', 'Not documented')}",
                    f"Price: {property_data.get('price', 'Not documented')}",
                    f"Location: {property_data.get('location', 'Not documented')}",
                    f"Payment: {property_data.get('payment_plan', 'Not documented')}",
                    f"Availability: {property_data.get('availability', 'Not documented')}",
                    f"Amenities: {', '.join(property_data.get('amenities') or []) or 'Not documented'}",
                    "",
                ]
            )
    else:
        lines.extend(["No property match was verified.", ""])

    lines.extend(["AI DRAFT:", inquiry.get("draft") or "No AI draft. Review manually.", ""])
    lines.extend(
        [
            "STATUS:",
            (
                "ESCALATION REQUIRED — AI was prevented from guessing."
                if escalation
                else (
                    "READY FOR REVIEW"
                    if status == "awaiting_review"
                    else status.replace("_", " ").upper()
                )
            ),
        ]
    )
    if inquiry.get("escalation_reason"):
        lines.append(f"Reason: {inquiry['escalation_reason']}")
    if status == "sending":
        lines.extend(
            [
                "",
                "Delivery is uncertain. Check the buyer chat, then use:",
                f"/resolve_send {inquiry['inquiry_id']} delivered|not_delivered",
            ]
        )
    elif status == "send_failed" and not inquiry.get("send_resolution"):
        lines.extend(
            [
                "",
                "Delivery may be uncertain. Check the buyer chat, then use:",
                f"/resolve_send {inquiry['inquiry_id']} delivered|not_delivered",
            ]
        )
    elif status in {"draft_failed", "send_failed", "approved"}:
        lines.extend(
            [
                "",
                f"To send a reviewed manual reply: /send {inquiry['inquiry_id']} <message>",
            ]
        )
    return "\n".join(lines)


class ReviewQueue:
    def __init__(self, store: JSONLStore, founder_chat_id: str | int):
        self.store = store
        self.founder_chat_id = str(founder_chat_id)
        # Per-inquiry locks: a slow or retrying send for one inquiry must
        # not block unrelated founder actions on other inquiries. All
        # methods run on a single event loop, so the dict access below
        # never interleaves between the get and the set.
        self._inquiry_locks: dict[str, asyncio.Lock] = {}

    def _inquiry_lock(self, inquiry_id: str) -> asyncio.Lock:
        lock = self._inquiry_locks.get(inquiry_id)
        if lock is None:
            lock = asyncio.Lock()
            self._inquiry_locks[inquiry_id] = lock
        return lock

    def is_founder(self, actor_user_id: str | int | None, chat_id: str | int | None) -> bool:
        # The supported founder destination is a private Telegram chat: both values
        # must match, so another member in a shared/group chat cannot administer it.
        return (
            actor_user_id is not None
            and chat_id is not None
            and str(actor_user_id) == self.founder_chat_id
            and str(chat_id) == self.founder_chat_id
        )

    def _authorize(
        self,
        actor_user_id: str | int | None,
        chat_id: str | int | None,
        action: str,
    ) -> bool:
        if self.is_founder(actor_user_id, chat_id):
            return True
        self.store.append_event("UNAUTHORIZED_ACTION", action=action)
        return False

    async def _deliver(
        self,
        inquiry: dict[str, Any],
        text: str,
        send_message: SendFunction,
        *,
        action: str,
    ) -> dict[str, Any]:
        inquiry_id = inquiry["inquiry_id"]
        self.store.update_inquiry(
            inquiry_id,
            status="sending",
            send_started_at=now_iso(),
            send_resolution=None,
            send_resolved_at=None,
            send_resolved_by=None,
        )
        self.store.append_event("SEND_STARTED", inquiry_id, action=action)
        attempts = 0

        async def attempt_send():
            nonlocal attempts
            attempts += 1
            latest = self.store.get_inquiry(inquiry_id)
            self.store.update_inquiry(
                inquiry_id,
                send_attempts=int((latest or {}).get("send_attempts", 0)) + 1,
            )
            if attempts > 1:
                self.store.append_event(
                    "SEND_RETRY", inquiry_id, attempt=attempts, action=action
                )
            result = send_message(int(inquiry["telegram_user_id"]), text)
            if inspect.isawaitable(result):
                return await result
            return result

        try:
            await with_transient_retry_async(
                attempt_send,
                attempts=3,
                wait_min_seconds=4,
                wait_max_seconds=10,
            )
        except Exception as exc:
            updated = self.store.update_inquiry(
                inquiry_id,
                status="send_failed",
                send_error=exc.__class__.__name__,
            )
            self.store.append_event(
                "SEND_FAILED", inquiry_id, error=exc.__class__.__name__, action=action
            )
            return {"ok": False, "status": "send_failed", "inquiry": updated}

        updated = self.store.update_inquiry(
            inquiry_id,
            status="sent",
            sent_at=now_iso(),
            send_error=None,
        )
        self.store.append_event("SEND_SUCCESS", inquiry_id, action=action)
        return {"ok": True, "status": "sent", "inquiry": updated}

    async def approve_and_send(
        self,
        inquiry_id: str,
        *,
        actor_user_id: str | int | None,
        chat_id: str | int | None,
        send_message: SendFunction,
    ) -> dict[str, Any]:
        async with self._inquiry_lock(inquiry_id):
            if not self._authorize(actor_user_id, chat_id, "approve"):
                return {"ok": False, "status": "unauthorized"}
            inquiry = self.store.get_inquiry(inquiry_id)
            if not inquiry:
                return {"ok": False, "status": "not_found"}
            if inquiry.get("status") == "sent":
                return {"ok": False, "status": "already_sent", "inquiry": inquiry}
            if inquiry.get("status") in {"approved", "sending"}:
                return {"ok": False, "status": "already_in_progress", "inquiry": inquiry}
            if inquiry.get("status") != "awaiting_review":
                return {"ok": False, "status": "not_approvable", "inquiry": inquiry}
            draft = inquiry.get("draft")
            if (
                not isinstance(draft, str)
                or not draft.strip()
                or inquiry.get("requires_escalation")
            ):
                return {"ok": False, "status": "invalid_draft", "inquiry": inquiry}

            inquiry = self.store.update_inquiry(
                inquiry_id,
                status="approved",
                approved_at=now_iso(),
                approved_by=str(actor_user_id),
            )
            self.store.append_event("APPROVED", inquiry_id, actor=str(actor_user_id))
            return await self._deliver(inquiry, draft, send_message, action="approval")

    async def reject(
        self,
        inquiry_id: str,
        *,
        actor_user_id: str | int | None,
        chat_id: str | int | None,
    ) -> dict[str, Any]:
        async with self._inquiry_lock(inquiry_id):
            if not self._authorize(actor_user_id, chat_id, "reject"):
                return {"ok": False, "status": "unauthorized"}
            inquiry = self.store.get_inquiry(inquiry_id)
            if not inquiry:
                return {"ok": False, "status": "not_found"}
            if inquiry.get("status") in {"sent", "sending"}:
                return {"ok": False, "status": "already_sent_or_sending", "inquiry": inquiry}
            if inquiry.get("status") not in {
                "awaiting_review",
                "escalated",
                "draft_failed",
                "approved",
                "send_failed",
            }:
                return {"ok": False, "status": "not_rejectable", "inquiry": inquiry}
            updated = self.store.update_inquiry(
                inquiry_id,
                status="rejected",
                rejected_at=now_iso(),
                rejected_by=str(actor_user_id),
            )
            self.store.append_event("REJECTED", inquiry_id, actor=str(actor_user_id))
            return {"ok": True, "status": "rejected", "inquiry": updated}

    async def resolve_ambiguous_send(
        self,
        inquiry_id: str,
        outcome: str,
        *,
        actor_user_id: str | int | None,
        chat_id: str | int | None,
    ) -> dict[str, Any]:
        """Record a founder's inspection of a send interrupted in the `sending` state."""
        async with self._inquiry_lock(inquiry_id):
            if not self._authorize(actor_user_id, chat_id, "resolve_send"):
                return {"ok": False, "status": "unauthorized"}
            if outcome not in {"delivered", "not_delivered"}:
                return {"ok": False, "status": "invalid_outcome"}
            inquiry = self.store.get_inquiry(inquiry_id)
            if not inquiry:
                return {"ok": False, "status": "not_found"}
            if inquiry.get("status") not in {"sending", "send_failed"}:
                return {
                    "ok": False,
                    "status": "not_resolvable",
                    "inquiry": inquiry,
                }

            if outcome == "delivered":
                updated = self.store.update_inquiry(
                    inquiry_id,
                    status="sent",
                    sent_at=now_iso(),
                    send_error=None,
                    send_resolution="founder_confirmed_delivered",
                    send_resolved_at=now_iso(),
                    send_resolved_by=str(actor_user_id),
                )
                self.store.append_event(
                    "SEND_CONFIRMED_DELIVERED",
                    inquiry_id,
                    actor=str(actor_user_id),
                )
                return {
                    "ok": True,
                    "status": "sent",
                    "resolution": "delivered",
                    "inquiry": updated,
                }

            updated = self.store.update_inquiry(
                inquiry_id,
                status="send_failed",
                send_error="Founder confirmed not delivered",
                send_resolution="founder_confirmed_not_delivered",
                send_resolved_at=now_iso(),
                send_resolved_by=str(actor_user_id),
            )
            self.store.append_event(
                "SEND_CONFIRMED_NOT_DELIVERED",
                inquiry_id,
                actor=str(actor_user_id),
            )
            return {
                "ok": True,
                "status": "send_failed",
                "resolution": "not_delivered",
                "inquiry": updated,
            }

    async def manual_send(
        self,
        inquiry_id: str,
        text: str,
        *,
        actor_user_id: str | int | None,
        chat_id: str | int | None,
        send_message: SendFunction,
    ) -> dict[str, Any]:
        async with self._inquiry_lock(inquiry_id):
            if not self._authorize(actor_user_id, chat_id, "manual_send"):
                return {"ok": False, "status": "unauthorized"}
            if not text or not text.strip():
                return {"ok": False, "status": "empty_message"}
            inquiry = self.store.get_inquiry(inquiry_id)
            if not inquiry:
                return {"ok": False, "status": "not_found"}
            if inquiry.get("status") in {"sent", "sending"}:
                return {"ok": False, "status": "already_sent_or_sending", "inquiry": inquiry}
            if inquiry.get("status") == "send_failed" and inquiry.get(
                "send_resolution"
            ) != "founder_confirmed_not_delivered":
                return {
                    "ok": False,
                    "status": "resolve_required",
                    "inquiry": inquiry,
                }
            if inquiry.get("status") not in {
                "awaiting_review",
                "escalated",
                "draft_failed",
                "send_failed",
                "approved",
            }:
                return {"ok": False, "status": "not_sendable", "inquiry": inquiry}

            text = text.strip()
            inquiry = self.store.update_inquiry(
                inquiry_id,
                draft=text,
                requires_escalation=False,
                status="approved",
                approved_at=now_iso(),
                approved_by=str(actor_user_id),
                manual_response=True,
            )
            self.store.append_event("MANUAL_EDIT", inquiry_id, actor=str(actor_user_id))
            self.store.append_event("APPROVED", inquiry_id, actor=str(actor_user_id), manual=True)
            return await self._deliver(inquiry, text, send_message, action="manual_send")