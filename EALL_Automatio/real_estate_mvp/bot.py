"""Telegram long-polling adapter and durable inquiry ingestion pipeline."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .draft import DraftGenerationError, generate_draft
from .knowledge import find_relevant_properties, get_property_context
from .queue import ReviewQueue, format_review_message
from .ratelimit import RateLimiter
from .retry import is_transient_error, with_transient_retry_async
from .storage import DuplicateInquiryError, JSONLStore
from .utils import BUSINESS_TZ, clip_text

logger = logging.getLogger(__name__)
BUYER_ACK = "Thanks for your inquiry. Your message has been received and is being reviewed."
RATE_LIMITED_ACK = (
    "Thanks for your interest. We are receiving many messages from you "
    "right now. Please wait a few minutes before sending again, or contact "
    "our office directly."
)
MAX_DRAFT_INPUT_CHARS = 4000


async def _safe_reply(message, text: str) -> bool:
    """Reply to the founder, retrying transient Telegram errors.

    Founder-facing replies must never crash a handler. The durable
    state transition has already happened by the time this runs, so a
    failed reply is logged and reported to the founder as a safe
    fallback rather than an unhandled exception.
    """
    async def attempt():
        result = message.reply_text(text)
        # Telegram's reply_text is async, but tolerate a plain callable
        # so the helper is robust in tests.
        if inspect.isawaitable(result):
            return await result
        return result

    try:
        await with_transient_retry_async(
            attempt,
            attempts=3,
            wait_min_seconds=1,
            wait_max_seconds=3,
        )
        return True
    except Exception as exc:
        logger.error(
            "[ERROR] founder reply failed (%s): %s",
            exc.__class__.__name__,
            text[:120],
        )
        return False


def _prepare_review_keyboard(
    inquiry: dict[str, Any],
) -> InlineKeyboardMarkup | None:
    inquiry_id = inquiry["inquiry_id"]
    if inquiry.get("status") in {"sent", "rejected"}:
        return None
    if inquiry.get("status") == "sending" or (
        inquiry.get("status") == "send_failed" and not inquiry.get("send_resolution")
    ):
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Resolve Send Status",
                        callback_data=f"review:resolve:{inquiry_id}",
                    )
                ]
            ]
        )
    rows = []
    if inquiry.get("status") == "awaiting_review" and not inquiry.get(
        "requires_escalation"
    ):
        rows.append(
            [InlineKeyboardButton("Approve & Send", callback_data=f"review:approve:{inquiry_id}")]
        )
    rows.extend(
        [
            [InlineKeyboardButton("Edit Manually", callback_data=f"review:edit:{inquiry_id}")],
            [InlineKeyboardButton("Reject", callback_data=f"review:reject:{inquiry_id}")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def prepare_inquiry(
    store: JSONLStore,
    settings: Settings,
    *,
    telegram_user_id: int | str | None,
    telegram_message_id: int | str | None,
    message: str,
    received_at: str | None = None,
    telegram_chat_id: int | str | None = None,
) -> dict[str, Any]:
    """Persist first, then perform deterministic matching and draft generation."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Buyer message must contain text")

    try:
        inquiry = store.create_inquiry(
            telegram_user_id=telegram_user_id,
            telegram_message_id=telegram_message_id,
            message=message,
            received_at=received_at,
            telegram_chat_id=telegram_chat_id,
        )
    except DuplicateInquiryError as exc:
        # Telegram redelivered a message that is already durable: do not
        # create a second record, a second draft, or a second founder
        # notification. The persisted record is the source of truth.
        inquiry = exc.inquiry
        store.append_event(
            "DUPLICATE_DELIVERY",
            inquiry["inquiry_id"],
            telegram_message_id=str(telegram_message_id),
        )
        logger.info(
            "[DUPLICATE] redelivered message for %s", inquiry["inquiry_id"]
        )
        return inquiry
    inquiry_id = inquiry["inquiry_id"]
    store.append_event("INQUIRY_RECEIVED", inquiry_id)
    logger.info("[RECEIVED] %s", inquiry_id)
    return await _process_inquiry_record(store, settings, inquiry)


async def _process_inquiry_record(
    store: JSONLStore,
    settings: Settings,
    inquiry: dict[str, Any],
) -> dict[str, Any]:
    """Run matching and drafting for an inquiry that is already durable."""
    inquiry_id = inquiry["inquiry_id"]
    message = inquiry["message"]
    try:
        matched_ids = find_relevant_properties(message)
        property_context = [
            property_data
            for property_id in matched_ids
            if (property_data := get_property_context(property_id)) is not None
        ]
        store.update_inquiry(
            inquiry_id,
            status="matched",
            matched_properties=matched_ids,
            property_context=property_context,
        )
        store.append_event(
            "PROPERTY_MATCHED", inquiry_id, matched_properties=matched_ids
        )
        logger.info("[MATCHED] %s count=%d", inquiry_id, len(matched_ids))

        store.update_inquiry(inquiry_id, status="drafting")
        store.append_event("DRAFT_STARTED", inquiry_id)
        if len(message) > MAX_DRAFT_INPUT_CHARS:
            raise DraftGenerationError("Buyer message exceeds the safe draft limit")
        result = await asyncio.to_thread(
            generate_draft,
            message,
            property_context,
            mock_mode=settings.mock_mode,
            api_key=settings.gemini_api_key or None,
            model=settings.gemini_model,
            temperature=settings.gemini_temperature,
            max_output_tokens=settings.gemini_max_output_tokens,
            timeout_seconds=settings.gemini_timeout_seconds,
        )
    except Exception as exc:
        failed = store.update_inquiry(
            inquiry_id,
            status="draft_failed",
            draft=None,
            draft_error=exc.__class__.__name__,
            requires_escalation=False,
        )
        store.append_event(
            "DRAFT_FAILED", inquiry_id, error=exc.__class__.__name__
        )
        logger.error(
            "[ERROR] unexpected draft failure for %s (%s)",
            inquiry_id,
            exc.__class__.__name__,
        )
        return failed

    status = "escalated" if result.requires_escalation else "awaiting_review"
    prepared = store.update_inquiry(
        inquiry_id,
        status=status,
        draft=result.text,
        requires_escalation=result.requires_escalation,
        escalation_reason=result.reason,
        draft_model=result.model,
    )
    if result.requires_escalation:
        store.append_event(
            "ESCALATION",
            inquiry_id,
            reason=result.reason or "Unknown Property Parameter",
        )
        logger.info("[ESCALATION] %s", inquiry_id)
    else:
        store.append_event("DRAFT_CREATED", inquiry_id, model=result.model)
        store.append_event("AWAITING_REVIEW", inquiry_id)
        logger.info("[REVIEW] %s", inquiry_id)
    return prepared


async def _notify_founder(
    application: Application,
    settings: Settings,
    store: JSONLStore,
    inquiry: dict[str, Any],
    *,
    edit_existing: bool = False,
) -> None:
    text = clip_text(format_review_message(inquiry), 3500)
    reply_markup = _prepare_review_keyboard(inquiry)
    if edit_existing and inquiry.get("review_message_id"):
        async def edit_review():
            return await application.bot.edit_message_text(
                chat_id=int(settings.founder_chat_id),
                message_id=int(inquiry["review_message_id"]),
                text=text,
                reply_markup=reply_markup,
            )

        try:
            await with_transient_retry_async(edit_review)
            return
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning(
                "[WARN] unable to update review message for %s; sending a new one",
                inquiry["inquiry_id"],
            )
        except Exception as exc:
            logger.warning(
                "[WARN] unable to update review message for %s (%s); sending a new one",
                inquiry["inquiry_id"],
                exc.__class__.__name__,
            )
    try:
        async def send_review():
            return await application.bot.send_message(
                chat_id=int(settings.founder_chat_id),
                text=text,
                reply_markup=reply_markup,
            )

        message = await with_transient_retry_async(
            send_review
        )
        store.update_inquiry(inquiry["inquiry_id"], review_message_id=message.message_id)
    except Exception as exc:
        store.append_event(
            "REVIEW_NOTIFICATION_FAILED",
            inquiry["inquiry_id"],
            error=exc.__class__.__name__,
        )
        logger.error(
            "[ERROR] founder notification failed for %s (%s)",
            inquiry["inquiry_id"],
            exc.__class__.__name__,
        )


async def recover_pending_inquiries(application: Application) -> None:
    """Resume interrupted drafting and refresh founder reviews without buyer sends."""
    settings: Settings = application.bot_data["settings"]
    store: JSONLStore = application.bot_data["store"]
    for inquiry in store.get_pending_inquiries():
        status = inquiry.get("status")
        if status in {"received", "matched", "drafting"}:
            logger.warning("[RECOVERY] resuming draft for %s", inquiry["inquiry_id"])
            prepared = await _process_inquiry_record(store, settings, inquiry)
            await _notify_founder(application, settings, store, prepared)
        elif status in {
            "awaiting_review",
            "escalated",
            "approved",
            "sending",
            "send_failed",
            "draft_failed",
        }:
            if status in {"approved", "sending", "send_failed"} or not inquiry.get(
                "review_message_id"
            ):
                logger.warning(
                    "[RECOVERY] refreshing founder review for %s [%s]",
                    inquiry["inquiry_id"],
                    status,
                )
                await _notify_founder(
                    application, settings, store, inquiry, edit_existing=True
                )


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_message:
        await _safe_reply(
            update.effective_message,
            "Welcome. Send a text inquiry about a property and the team will review it.",
        )


async def buyer_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    settings: Settings = context.application.bot_data["settings"]
    store: JSONLStore = context.application.bot_data["store"]
    if str(chat.id) == settings.founder_chat_id:
        await _safe_reply(message, "For a manual buyer response, use /send <inquiry_id> <message>.")
        return
    text = message.text or ""
    if not text.strip():
        await _safe_reply(message, "Please send your question as a text message.")
        return

    # Process-local anti-flood protection. Rate-limited messages are
    # answered safely and are not persisted as inquiries, so flooding
    # cannot trigger founder-notification storms.
    rate_limiter: RateLimiter | None = context.application.bot_data.get(
        "rate_limiter"
    )
    sender_key = str(chat.id)
    if rate_limiter is not None and not rate_limiter.allow(sender_key):
        store.append_event("RATE_LIMITED", sender=sender_key)
        logger.warning("[RATE_LIMITED] sender %s", sender_key)
        await _safe_reply(message, RATE_LIMITED_ACK)
        return

    # Persist before acknowledging so a process restart cannot lose a
    # received inquiry. Redelivered Telegram messages are deduplicated
    # against the durable record.
    try:
        received_at = (
            message.date.astimezone(BUSINESS_TZ).isoformat()
            if message.date
            else None
        )
        inquiry = store.create_inquiry(
            telegram_user_id=user.id,
            telegram_message_id=message.message_id,
            message=text,
            received_at=received_at,
            telegram_chat_id=chat.id,
        )
    except DuplicateInquiryError as exc:
        # Telegram redelivered a message that is already durable. This
        # happens when the process died between persisting and
        # acknowledging, so acknowledge again rather than leaving the
        # buyer silent. No second record, draft, or notification is
        # created.
        store.append_event(
            "DUPLICATE_DELIVERY",
            exc.inquiry["inquiry_id"],
            telegram_message_id=str(message.message_id),
        )
        logger.info(
            "[DUPLICATE] redelivered message for %s", exc.inquiry["inquiry_id"]
        )
        await _safe_reply(message, BUYER_ACK)
        return
    except Exception as exc:
        logger.error("[ERROR] unable to persist buyer inquiry (%s)", exc.__class__.__name__)
        await _safe_reply(
            message,
            "We could not record your message right now. Please try again shortly.",
        )
        return
    store.append_event("INQUIRY_RECEIVED", inquiry["inquiry_id"])
    logger.info("[RECEIVED] %s", inquiry["inquiry_id"])
    await _safe_reply(message, BUYER_ACK)

    # Continue with the already-created inquiry instead of creating a second record.
    await _finish_inquiry(context.application, settings, store, inquiry)


async def _finish_inquiry(
    application: Application,
    settings: Settings,
    store: JSONLStore,
    inquiry: dict[str, Any],
) -> dict[str, Any]:
    prepared = await _process_inquiry_record(store, settings, inquiry)
    await _notify_founder(application, settings, store, prepared)
    return prepared


async def founder_command_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Abort an in-progress manual edit without sending anything.

    The founder may start typing an edited reply and change their mind;
    this gives them a clean way to abort rather than sending a wrong
    message or leaving the inquiry in an ambiguous state.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    queue: ReviewQueue = context.application.bot_data["queue"]
    store: JSONLStore = context.application.bot_data["store"]
    if not queue.is_founder(user.id, chat.id):
        store.append_event("UNAUTHORIZED_ACTION", action="cancel_command")
        await _safe_reply(message, "This command is not available.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await _safe_reply(message, "Usage: /cancel <inquiry_id>")
        return
    inquiry_id = parts[1]
    inquiry = store.get_inquiry(inquiry_id)
    if not inquiry:
        await _safe_reply(message, f"Inquiry {inquiry_id} not found.")
        return
    if inquiry.get("status") not in {"awaiting_review", "escalated", "draft_failed"}:
        await _safe_reply(
            message,
            f"Inquiry {inquiry_id} is {inquiry['status'].replace('_', ' ')}; nothing to cancel.",
        )
        return
    result = await queue.reject(inquiry_id, actor_user_id=user.id, chat_id=chat.id)
    await _safe_reply(
        message, f"Inquiry {inquiry_id} cancelled ({result['status']}). No message was sent."
    )


async def founder_command_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    queue: ReviewQueue = context.application.bot_data["queue"]
    store: JSONLStore = context.application.bot_data["store"]
    if not queue.is_founder(user.id, chat.id):
        store.append_event("UNAUTHORIZED_ACTION", action="manual_send_command")
        await _safe_reply(message, "This command is not available.")
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await _safe_reply(
            message,
            "Usage: /send <inquiry_id> <message>\n"
            "Example: /send inq_000001 It is listed at 12,000,000 ETB.\n"
            "Abort an edit with /cancel <inquiry_id>.",
        )
        return
    inquiry_id, reply_text = parts[1], parts[2]
    result = await queue.manual_send(
        inquiry_id,
        reply_text,
        actor_user_id=user.id,
        chat_id=chat.id,
        send_message=lambda buyer_chat_id, text: context.bot.send_message(
            chat_id=buyer_chat_id, text=text
        ),
    )
    await _safe_reply(message, f"Manual send: {result['status'].replace('_', ' ')}.")
    updated_inquiry = result.get("inquiry")
    if updated_inquiry:
        await _notify_founder(
            context.application, context.application.bot_data["settings"], store,
            updated_inquiry, edit_existing=True,
        )


async def founder_command_resolve_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return
    queue: ReviewQueue = context.application.bot_data["queue"]
    store: JSONLStore = context.application.bot_data["store"]
    if not queue.is_founder(user.id, chat.id):
        store.append_event("UNAUTHORIZED_ACTION", action="resolve_send_command")
        await _safe_reply(message, "This command is not available.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await _safe_reply(
            message,
            "Usage: /resolve_send <inquiry_id> delivered|not_delivered\n"
            "Check the buyer chat before choosing.",
        )
        return
    result = await queue.resolve_ambiguous_send(
        parts[1],
        parts[2],
        actor_user_id=user.id,
        chat_id=chat.id,
    )
    await _safe_reply(
        message, f"Send resolution: {result['status'].replace('_', ' ')}."
    )
    inquiry = result.get("inquiry")
    if inquiry:
        await _notify_founder(
            context.application,
            context.application.bot_data["settings"],
            store,
            inquiry,
            edit_existing=True,
        )


async def _safe_query_answer(query, text: str, *, show_alert: bool = False) -> bool:
    """Acknowledge a callback query, tolerating transient Telegram errors."""
    try:
        await with_transient_retry_async(
            lambda: query.answer(text, show_alert=show_alert),
            attempts=2,
            wait_min_seconds=1,
            wait_max_seconds=2,
        )
        return True
    except Exception as exc:
        logger.error(
            "[ERROR] callback answer failed (%s): %s",
            exc.__class__.__name__,
            text[:120],
        )
        return False


async def _safe_send_message(bot, chat_id: int, text: str) -> bool:
    """Send a founder-facing message, tolerating transient Telegram errors."""
    try:
        await with_transient_retry_async(
            lambda: bot.send_message(chat_id=chat_id, text=text),
            attempts=2,
            wait_min_seconds=1,
            wait_max_seconds=2,
        )
        return True
    except Exception as exc:
        logger.error(
            "[ERROR] founder message failed (%s): %s",
            exc.__class__.__name__,
            text[:120],
        )
        return False


async def review_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if not query or not user or not chat:
        return
    store: JSONLStore = context.application.bot_data["store"]
    queue: ReviewQueue = context.application.bot_data["queue"]
    match = re.fullmatch(
        r"review:(approve|edit|reject|resolve):(inq_\d{6,})", query.data or ""
    )
    if not match:
        await _safe_query_answer(query, "This action is no longer available.", show_alert=True)
        return
    action, inquiry_id = match.groups()
    if not queue.is_founder(user.id, chat.id):
        store.append_event("UNAUTHORIZED_ACTION", action=f"review_{action}")
        await _safe_query_answer(query, "Unauthorized.", show_alert=True)
        return

    if action == "edit":
        await _safe_query_answer(query, "Manual review required.")
        await _safe_send_message(
            context.bot, chat.id,
            "Review the facts, then send your approved reply:\n"
            f"/send {inquiry_id} <your approved reply>\n"
            f"Abort without sending: /cancel {inquiry_id}",
        )
        return
    if action == "resolve":
        await _safe_query_answer(query, "Check the buyer chat before resolving.")
        await _safe_send_message(
            context.bot, chat.id,
            (
                f"Delivery is uncertain for {inquiry_id}. Check the buyer chat, then send:\n"
                f"/resolve_send {inquiry_id} delivered\n"
                f"or /resolve_send {inquiry_id} not_delivered\n"
                "This command changes the saved status but does not message the buyer."
            ),
        )
        return
    if action == "approve":
        result = await queue.approve_and_send(
            inquiry_id,
            actor_user_id=user.id,
            chat_id=chat.id,
            send_message=lambda buyer_chat_id, text: context.bot.send_message(
                chat_id=buyer_chat_id, text=text
            ),
        )
    else:
        result = await queue.reject(
            inquiry_id, actor_user_id=user.id, chat_id=chat.id
        )
    await _safe_query_answer(query, result["status"].replace("_", " ").capitalize())
    inquiry = store.get_inquiry(inquiry_id)
    if inquiry:
        updated_text = format_review_message(inquiry)
        if result["status"] == "sent":
            await _safe_reply(query.message, updated_text)
        elif result["status"] == "rejected":
            try:
                await query.edit_message_text(updated_text, reply_markup=None)
            except Exception as exc:
                logger.error("[ERROR] edit message failed (%s)", exc.__class__.__name__)
        elif result["status"] == "send_failed":
            try:
                await query.edit_message_text(
                    clip_text(updated_text, 3500),
                    reply_markup=_prepare_review_keyboard(inquiry),
                )
            except Exception as exc:
                logger.error("[ERROR] edit message failed (%s)", exc.__class__.__name__)
        elif inquiry.get("status") == "sending":
            try:
                await query.edit_message_text(
                    clip_text(updated_text, 3500),
                    reply_markup=_prepare_review_keyboard(inquiry),
                )
            except Exception as exc:
                logger.error("[ERROR] edit message failed (%s)", exc.__class__.__name__)


async def unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_message:
        await _safe_reply(
            update.effective_message,
            "Unsupported command. Send a text inquiry or use /start.",
        )


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Suppress noisy library tracebacks for transient network errors.

    python-telegram-bot prints "No error handlers are registered" with a
    full traceback on every transient network blip, even though the
    library recovers on its own. Log those compactly instead; surface
    unexpected errors loudly so they are not silently swallowed.
    """
    error = getattr(context.error, "__class__", type(context.error))
    if isinstance(context.error, (NetworkError, TimedOut, asyncio.TimeoutError)):
        logger.warning("[NETWORK] transient Telegram error (%s); library recovers", error.__name__)
        return
    logger.error("[ERROR] unhandled update error (%s)", error.__name__, exc_info=context.error)


def build_application(settings: Settings) -> Application:
    settings.validate()
    if settings.mock_mode:
        raise ValueError("Mock mode does not start Telegram polling.")
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(recover_pending_inquiries)
        .build()
    )
    application.add_error_handler(_error_handler)
    store = JSONLStore(settings.data_dir)
    queue = ReviewQueue(store, settings.founder_chat_id)
    rate_limiter = RateLimiter(
        max_per_hour=settings.rate_limit_per_hour,
        max_per_day=settings.rate_limit_per_day,
    )
    application.bot_data.update(
        settings=settings, store=store, queue=queue, rate_limiter=rate_limiter
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("send", founder_command_send))
    application.add_handler(CommandHandler("cancel", founder_command_cancel))
    application.add_handler(
        CommandHandler("resolve_send", founder_command_resolve_send)
    )
    application.add_handler(
        CallbackQueryHandler(review_callback, pattern=r"^review:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, buyer_text)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    return application