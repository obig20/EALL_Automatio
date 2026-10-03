"""CLI entry point for mock operation, simulation, and live Telegram polling."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .bot import build_application, prepare_inquiry
from .config import get_settings
from .queue import ReviewQueue
from .simulation import run_stress_simulation
from .storage import JSONLStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _print_pending(store: JSONLStore) -> None:
    pending = store.get_pending_inquiries()
    if not pending:
        print("No pending inquiries.")
        return
    print(f"Pending inquiries: {len(pending)}")
    for inquiry in pending:
        print(
            f"- {inquiry['inquiry_id']} [{inquiry['status']}] "
            f"{inquiry.get('message', '')[:90]}"
        )
    print("Pending items are not sent automatically after startup.")


async def _mock_console(settings, store: JSONLStore) -> None:
    queue = ReviewQueue(store, founder_chat_id="local-founder")
    print("ASTER mock console — synthetic data only; no external calls.")
    print(
        "Enter a buyer message, /pending, /approve ID, /reject ID, "
        "/send ID TEXT, /resolve_send ID delivered|not_delivered, or /exit."
    )
    _print_pending(store)

    async def mock_delivery(chat_id: int, text: str) -> None:
        print(f"\n[MOCK ONLY] Response to synthetic Telegram user {chat_id}:")
        print(text)

    while True:
        try:
            raw = input("\nASTER> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting mock console.")
            return
        if not raw:
            continue
        if raw in {"/exit", "/quit"}:
            return
        if raw == "/pending":
            _print_pending(store)
            continue
        parts = raw.split(maxsplit=2)
        command = parts[0]
        if command == "/approve" and len(parts) >= 2:
            result = await queue.approve_and_send(
                parts[1],
                actor_user_id="local-founder",
                chat_id="local-founder",
                send_message=mock_delivery,
            )
            print(f"Approval: {result['status']}")
            continue
        if command == "/reject" and len(parts) >= 2:
            result = await queue.reject(
                parts[1],
                actor_user_id="local-founder",
                chat_id="local-founder",
            )
            print(f"Rejection: {result['status']}")
            continue
        if command == "/send" and len(parts) == 3:
            result = await queue.manual_send(
                parts[1],
                parts[2],
                actor_user_id="local-founder",
                chat_id="local-founder",
                send_message=mock_delivery,
            )
            print(f"Manual send: {result['status']}")
            continue
        if command == "/resolve_send" and len(parts) == 3:
            result = await queue.resolve_ambiguous_send(
                parts[1],
                parts[2],
                actor_user_id="local-founder",
                chat_id="local-founder",
            )
            print(f"Send resolution: {result['status']}")
            continue
        if command.startswith("/"):
            print("Unknown command.")
            continue

        inquiry = await prepare_inquiry(
            store,
            settings,
            telegram_user_id=9_000_001,
            telegram_message_id=len(store.list_inquiries()) + 1,
            message=raw,
        )
        print(f"\nInquiry {inquiry['inquiry_id']} — {inquiry['status']}")
        print(f"Draft: {inquiry.get('draft') or 'No draft; review manually.'}")
        if inquiry.get("requires_escalation"):
            print("Unknown facts were not guessed; this inquiry requires manual handling.")
        print("Nothing is sent to a buyer until you explicitly approve or use /send.")


def main() -> None:
    parser = argparse.ArgumentParser(description="ASTER real-estate inquiry MVP")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="run a credential-free 20-inquiry stress/acceptance simulation",
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="list durable pending inquiries and exit",
    )
    args = parser.parse_args()
    if args.simulate:
        summary = run_stress_simulation()
        print("ASTER MOCK ACCEPTANCE SIMULATION: PASS")
        print(f"Synthetic inquiries: {summary['synthetic_inquiries']}")
        print(f"Persisted statuses: {summary['statuses']}")
        print(f"Founder deliveries in simulation: {summary['founder_deliveries']}")
        print(f"Transient retry attempts: {summary['retry_attempts']}")
        print(f"Pending after restart: {summary['pending_after_restart']}")
        print(f"Logged events: {summary['event_count']}")
        for check in summary["checks"]:
            print(f"  PASS — {check}")
        return

    settings = get_settings()
    store = JSONLStore(settings.data_dir)
    if args.pending:
        _print_pending(store)
        return

    pending = store.get_pending_inquiries()
    if pending:
        logger.warning(
            "[RECOVERY] %d pending inquiries found; none will be sent automatically",
            len(pending),
        )
    if settings.mock_mode:
        asyncio.run(_mock_console(settings, store))
        return

    application = build_application(settings)
    logger.info("[SUCCESS] Telegram polling started; AI drafts require founder approval")
    application.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()