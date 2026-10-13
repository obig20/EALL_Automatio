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

## Runtime data directories (mock vs live)

Mock and live traffic never share a runtime directory by default:

- Mock mode (default): `real_estate_mvp/data/mock/`
- Live mode (`MOCK_MODE=false`): `real_estate_mvp/data/live/`

Static demo data (`data/properties.json`) stays tracked in Git; runtime
state (`inquiries.jsonl`, `events.jsonl`, generated reports) is always
git-ignored. `DATA_DIR` overrides the mode-specific default, and starting
in mock mode while pointed at the live directory logs a loud warning.
The startup line prints the active mode and runtime directory:

```
[CONFIG] mode=mock runtime_data=.../real_estate_mvp/data/mock properties=.../properties.json
```

The test suite and the acceptance simulation always use temporary
directories and never touch mock or live runtime data.

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

Live drafting settings (all optional, all env-configured):

- `GEMINI_MODEL` — default `gemini-2.5-flash`
- `GEMINI_TEMPERATURE` — default `0.0`
- `GEMINI_MAX_OUTPUT_TOKENS` — default `1024`
- `GEMINI_TIMEOUT_SECONDS` — default `30`

### Draft validation boundary (honest statement)

Every generated draft — mock and live — passes a deterministic
validator before it reaches the founder:

1. Every number in the draft must exist in the verified property data.
2. Bedroom counts must match the verified record exactly.
3. Marketing, urgency, guarantee, and advice phrases are always rejected.
4. Fact-bearing vocabulary (amenities, location, financing,
   availability, legal terms) must be supported by the verified data.

The validator is deliberately conservative: if a claim cannot be
confidently grounded, the draft is escalated for manual handling.
It does not prove natural-language truth — it prevents unsupported
claims from reaching review. A founder should still read every
draft before approving; ASTER does not guarantee perfect LLM
factual verification.

## Telegram inbound deduplication

Inbound messages are deduplicated on `(Telegram chat ID, message
ID)`, persisted in the inquiry record. A redelivery — for example
after a process restart or polling replay — never creates a second
inquiry, a second AI draft, or a second founder notification; it
only re-acknowledges the buyer. Deduplication state is read from
durable storage, so it remains correct after restarts.

## Rate limiting (anti-flood)

A process-local sliding-window limiter protects against one sender
flooding the bot (defaults: `RATE_LIMIT_PER_HOUR=10`,
`RATE_LIMIT_PER_DAY=50`; `0` disables a limit). Rate-limited
senders receive a safe reply and their messages are not persisted
as inquiries, so floods cannot trigger founder-notification storms.
This is process-local protection, not a distributed rate limiter:
state resets on restart and does not cover multiple bot instances.

## WhatsApp response-time audit

The included chat file contains only synthetic messages:

```bash
python -m real_estate_mvp.audit \
  real_estate_mvp/sample_data/sample_whatsapp_export.txt
```

The command prints overall and coverage-period statistics and writes JSON to `real_estate_mvp/reports/audit.json`. Set `AGENT_NAMES` to a comma-separated list of agent sender labels if your export uses different names. The parser supports common day-first, month-first, and ISO-style dates, 12/24-hour times, and multiline message bodies.

Date handling is explicit and configurable:

- `AUDIT_DATE_ORDER` — `day_first` (default, matching the operating
  context), `month_first`, `year_first`, or `auto`. `auto` requires
  unambiguous evidence (a date component greater than 12) and
  refuses to guess when every date could be either reading.
- `AUDIT_TIMEZONE` — IANA timezone used to interpret export
  timestamps and classify coverage periods (default
  `Africa/Nairobi`).

Ambiguous or unparseable timestamps fail with a clear error
instead of being silently reinterpreted.

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

Founder authorization requires the Telegram user ID and the
private chat ID to both match `FOUNDER_CHAT_ID`; group chats and
other users cannot approve, reject, edit, send, or resolve
anything, and attempts are logged without buyer content. This is
single-account authorization — it is not equivalent to enterprise
MFA, and compromise of the founder's Telegram account compromises
the approval gate.

## Limitations

- Keyword matching is deterministic and intentionally small; ambiguous matches are escalated.
- The JSONL store is appropriate for a single-process demonstration, not multi-instance traffic or high write volume.
- Human approval is required, but a network failure after Telegram accepts a message can leave delivery status uncertain. The app will not automatically resend a persisted `sending` record.
- Telegram and live Gemini cannot be verified without the user's configured credentials. No live calls are needed for the mock suite.
- Draft validation is conservative and deterministic; it blocks unsupported claims but does not prove full factual correctness.
- Rate limiting is process-local; it is not distributed protection.

## TypeScript/Node workspace

The `lib/` and `artifacts/` pnpm workspace (Express API server
with a health endpoint, Drizzle DB scaffold, generated Zod
schemas, and a mockup sandbox) is original template scaffold.
It is not used by the Python MVP, contains no ASTER runtime
logic, and is retained only as workspace context. The Python
application is the MVP.