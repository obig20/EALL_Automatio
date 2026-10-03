# ASTER — Human-Supervised Real-Estate Inquiry MVP

A Python MVP for capturing text inquiries through Telegram, matching them against a deterministic property file, preparing a grounded response draft, and placing it in a founder-only review queue. It also includes an offline response-time audit for synthetic or anonymized WhatsApp text exports.

**Safety rule:** AI never sends a buyer-facing message. Only the configured founder can approve a draft or send a manually written reply.

## Architecture

```text
Buyer
  ↓
Telegram long-polling bot
  ↓
Persist inquiry to JSONL
  ↓
Deterministic property knowledge base
  ↓
Gemini draft (or deterministic mock)
  ↓
Founder-only review queue
  ↓
Explicit human approval or manual reply
  ↓
Telegram response to buyer
```

WhatsApp is used only as an offline `.txt` audit input. This project does not call the WhatsApp, WhatsApp Business, or Meta APIs.

## Requirements and installation

- Python 3.11+
- No database; inquiry state and audit events are local JSONL files.

From the workspace root:

```bash
python -m pip install -r real_estate_mvp/requirements.txt
```

Run the test suite:

```bash
python -m pytest -q real_estate_mvp/tests
```

## Mock mode

Mock mode is the default and needs no Telegram or Gemini credentials:

```bash
MOCK_MODE=true python -m real_estate_mvp.main
```

It opens a local console where you can enter synthetic buyer messages and review them with `/approve <inquiry_id>`, `/reject <inquiry_id>`, or `/send <inquiry_id> <message>`. Mock replies are deterministic. They do not use an LLM or send Telegram messages.

Run the 20-inquiry acceptance/stress simulation:

```bash
python -m real_estate_mvp.main --simulate
```

## Telegram and founder setup

Live polling is disabled in mock mode. To test Telegram:

1. Create a bot with Telegram's `@BotFather` and copy its bot token into Replit Secrets as `TELEGRAM_BOT_TOKEN` (or into the ignored local `.env` file).
2. Start a private chat with the bot and send `/start`.
3. Configure the founder's numeric private-chat ID as `FOUNDER_CHAT_ID`. It must be a private founder chat; group chat IDs are not accepted for review actions.
4. Set `MOCK_MODE=false` only when you intend to make live service calls. The founder still has to approve every draft before it can be delivered.

Do not put tokens or API keys in source code, chat, or committed files. `.env` is ignored by Git; use Replit Secrets for live credentials.

## Gemini setup

Live drafts use the official Google GenAI Python SDK and `GEMINI_API_KEY`. Add the key to Replit Secrets (or the ignored local `.env`). Mock mode does not load or require the key. Gemini drafts are validated and still require human approval.

## WhatsApp response-time audit

The included chat file contains only synthetic messages:

```bash
python -m real_estate_mvp.audit \
  real_estate_mvp/sample_data/sample_whatsapp_export.txt
```

The command prints overall and coverage-period statistics and writes JSON to `real_estate_mvp/reports/audit.json`. Set `AGENT_NAMES` to a comma-separated list of agent sender labels if your export uses different names. The parser supports common day-first, month-first, and ISO-style dates, 12/24-hour times, and multiline message bodies.

Consecutive buyer messages within four hours count as one inquiry cycle. A longer buyer-only gap starts a separate cycle, so unanswered inquiries are not hidden by a much later response. Unanswered cycles remain in the denominator for the unanswered and >15-minute percentages. Sunday is reported separately; other messages are classified as business hours, uncovered hours, or night using the configured local hours (Africa/Nairobi).

## Persistence and recovery

- `data/inquiries.jsonl` stores the latest state for each inquiry.
- `data/events.jsonl` is append-only and records important state changes.
- State is written before buyer acknowledgement and updated atomically through a temporary file + rename.
- On live bot startup, interrupted draft generation is safely resumed and pending founder reviews are refreshed; no buyer message is automatically sent.
- A record left in `sending` or `send_failed` is deliberately not resent automatically. Telegram does not provide a message idempotency key, so a process crash or ambiguous network timeout can leave delivery uncertain. Check the buyer chat, then use `/resolve_send <inquiry_id> delivered|not_delivered`. Only after confirming non-delivery can the founder retry with `/send`.
- Transient API/network errors use bounded exponential retries. Permanent request/permission errors are not retried.

The files are designed for one running bot process and a small MVP workload, not concurrent multi-instance deployment.

## Security, compliance, and testing

- Only the private founder chat ID can approve, reject, or manually send.
- The buyer receives only a fixed acknowledgement before review; no AI-generated answer goes directly to them.
- Unknown property facts produce `[ESCALATE: Unknown Property Parameter]`.
- Demo listings are visibly marked **DEMO DATA** in the founder review message and are not current real listings.
- Start with synthetic inquiries, test Telegram accounts, anonymized exports, and non-sensitive demo property information. Do not connect real customer data until the appropriate legal/compliance review has been completed.

## Limitations

- Keyword matching is deterministic and intentionally small; ambiguous matches are escalated.
- The JSONL store is appropriate for a single-process demonstration, not multi-instance traffic or high write volume.
- Human approval is required, but a network failure after Telegram accepts a message can leave delivery status uncertain. The app will not automatically resend a persisted `sending` record after restart.
- Telegram and live Gemini cannot be verified without the user's configured credentials. No live calls are needed for the mock suite.