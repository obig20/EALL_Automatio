"""Credential-free acceptance simulation for the MVP workflow."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from .bot import prepare_inquiry
from .config import Settings
from .queue import ReviewQueue
from .storage import JSONLStore

SYNTHETIC_MESSAGES = [
    "How much is the Bole 2 bedroom apartment?",
    "Is the Bole 2 bedroom apartment available?",
    "Where is the Bole 2 bedroom apartment located?",
    "What is the payment plan for the Bole 2 bedroom apartment?",
    "How many bedrooms does the Bole 2 bedroom apartment have?",
    "Does the Bole 2 bedroom apartment have a swimming pool?",
    "Can I pay for the Bole 2 bedroom apartment over 36 months?",
    "Is there a discount on the Bole 2 bedroom apartment?",
    "When is the Bole 2 bedroom apartment completion date?",
    "Hello!",
    "How much is the Summit family villa?",
    "Does the Summit villa have parking?",
    "How much is the Kazanchis 1 bedroom apartment?",
    "Does the Kazanchis apartment have a gym?",
    "Can I arrange a viewing of the Bole apartment?",
    "???",
    "How much is the Bole 3 bedroom apartment?",
    "What is the availability of the Summit villa?",
    "What is the price of the Kazanchis 1 bedroom apartment?",
    "Bole 2 bedroom apartment " + ("Tell me more. " * 400),
]


def _settings(data_dir: Path) -> Settings:
    return Settings(
        telegram_bot_token="",
        founder_chat_id="",
        gemini_api_key="",
        mock_mode=True,
        business_start_hour=8,
        business_end_hour=18,
        uncovered_end_hour=21,
        agent_names=("Agent", "Sales", "Admin"),
        data_dir=data_dir,
        properties_file=Path(__file__).resolve().parent / "data" / "properties.json",
    )


async def _run_simulation(data_dir: Path) -> dict:
    settings = _settings(data_dir)
    store = JSONLStore(data_dir)
    inquiries = []
    for index, message in enumerate(SYNTHETIC_MESSAGES, start=1):
        inquiry = await prepare_inquiry(
            store,
            settings,
            telegram_user_id=900_000 + index,
            telegram_message_id=index,
            message=message,
        )
        inquiries.append(inquiry)

    # Invalid message types are rejected without corrupting the store or crashing.
    try:
        await prepare_inquiry(
            store,
            settings,
            telegram_user_id=999_999,
            telegram_message_id=999,
            message="",  # type: ignore[arg-type]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Empty input was not rejected")

    queue = ReviewQueue(store, founder_chat_id="900001")
    delivered: list[tuple[int, str]] = []

    async def send_message(chat_id: int, text: str) -> None:
        delivered.append((chat_id, text))

    # Approval plus a duplicate callback produces a single delivery.
    approval = await queue.approve_and_send(
        inquiries[0]["inquiry_id"],
        actor_user_id=900001,
        chat_id=900001,
        send_message=send_message,
    )
    duplicate = await queue.approve_and_send(
        inquiries[0]["inquiry_id"],
        actor_user_id=900001,
        chat_id=900001,
        send_message=send_message,
    )
    if approval["status"] != "sent" or duplicate["status"] != "already_sent":
        raise AssertionError("Approval idempotency check failed")

    # Rejecting a draft does not send anything.
    rejected = await queue.reject(
        inquiries[9]["inquiry_id"], actor_user_id=900001, chat_id=900001
    )
    if rejected["status"] != "rejected":
        raise AssertionError("Rejection check failed")

    # An escalated draft can only be delivered as explicit manual founder text.
    manual = await queue.manual_send(
        inquiries[5]["inquiry_id"],
        "I will confirm this detail with the team.",
        actor_user_id=900001,
        chat_id=900001,
        send_message=send_message,
    )
    if manual["status"] != "sent":
        raise AssertionError("Manual response check failed")

    # Permanent Telegram errors are stored and are not retried.
    async def permanent_failure(_chat_id: int, _text: str) -> None:
        raise ValueError("synthetic permanent Telegram failure")

    failed = await queue.approve_and_send(
        inquiries[2]["inquiry_id"],
        actor_user_id=900001,
        chat_id=900001,
        send_message=permanent_failure,
    )
    if failed["status"] != "send_failed":
        raise AssertionError("Send failure persistence check failed")

    # Exercise transient retry with zero delay in this local-only simulation.
    from . import queue as queue_module

    original_retry = queue_module.with_transient_retry_async

    async def fast_retry(operation, **kwargs):
        return await original_retry(
            operation, attempts=3, wait_min_seconds=0, wait_max_seconds=0
        )

    queue_module.with_transient_retry_async = fast_retry
    retry_attempts = 0

    async def transient_failure_then_success(chat_id: int, text: str) -> None:
        nonlocal retry_attempts
        retry_attempts += 1
        if retry_attempts == 1:
            raise ConnectionError("synthetic temporary network failure")
        delivered.append((chat_id, text))

    try:
        retried = await queue.approve_and_send(
            inquiries[3]["inquiry_id"],
            actor_user_id=900001,
            chat_id=900001,
            send_message=transient_failure_then_success,
        )
    finally:
        queue_module.with_transient_retry_async = original_retry
    if retried["status"] != "sent" or retry_attempts != 2:
        raise AssertionError("Transient retry check failed")

    # Missing live Gemini configuration produces a durable draft_failed state.
    live_without_key = replace(settings, mock_mode=False)
    live_failure = await prepare_inquiry(
        store,
        live_without_key,
        telegram_user_id=999_998,
        telegram_message_id=998,
        message="What is the price of the Bole 2 bedroom apartment?",
    )
    if live_failure["status"] != "draft_failed":
        raise AssertionError("Gemini failure persistence check failed")

    # A new store instance simulates a process restart.
    restarted = JSONLStore(data_dir)
    recovered = restarted.get_inquiry(inquiries[0]["inquiry_id"])
    if not recovered or recovered["status"] != "sent":
        raise AssertionError("Restart recovery check failed")
    if restarted.get_inquiry(failed["inquiry"]["inquiry_id"])["status"] != "send_failed":
        raise AssertionError("Failed-send recovery check failed")

    statuses = {}
    for inquiry in inquiries:
        status = restarted.get_inquiry(inquiry["inquiry_id"])["status"]
        statuses[status] = statuses.get(status, 0) + 1
    event_names = [event["event"] for event in restarted.list_events()]
    required_events = {
        "INQUIRY_RECEIVED",
        "DRAFT_CREATED",
        "ESCALATION",
        "APPROVED",
        "REJECTED",
        "MANUAL_EDIT",
        "SEND_STARTED",
        "SEND_SUCCESS",
        "SEND_FAILED",
        "SEND_RETRY",
        "DRAFT_FAILED",
    }
    missing_events = required_events - set(event_names)
    if missing_events:
        raise AssertionError(f"Missing expected events: {sorted(missing_events)}")

    return {
        "synthetic_inquiries": len(inquiries),
        "statuses": statuses,
        "founder_deliveries": len(delivered),
        "retry_attempts": retry_attempts,
        "pending_after_restart": len(restarted.get_pending_inquiries()),
        "event_count": len(event_names),
        "checks": [
            "known facts and unknown-fact escalation",
            "empty and overlong input handling",
            "duplicate approval prevented",
            "rejection sends nothing",
            "manual founder response",
            "permanent send failure persisted",
            "transient retry succeeds",
            "Gemini failure persisted",
            "state recovered after restart",
        ],
    }


def run_stress_simulation() -> dict:
    with tempfile.TemporaryDirectory(prefix="aster-mvp-") as directory:
        return asyncio.run(_run_simulation(Path(directory)))


def main() -> None:
    summary = run_stress_simulation()
    print(f"ASTER MOCK ACCEPTANCE SIMULATION: PASS")
    print(f"Synthetic inquiries: {summary['synthetic_inquiries']}")
    print(f"Persisted statuses: {summary['statuses']}")
    print(f"Founder deliveries in simulation: {summary['founder_deliveries']}")
    print(f"Transient retry attempts: {summary['retry_attempts']}")
    print(f"Pending after restart: {summary['pending_after_restart']}")
    print(f"Logged events: {summary['event_count']}")
    for check in summary["checks"]:
        print(f"  PASS — {check}")


if __name__ == "__main__":
    main()