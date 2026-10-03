# ASTER — Human-Supervised Real-Estate Inquiry MVP

ASTER captures real-estate questions through Telegram, drafts grounded replies from local property data, and requires founder approval before any buyer-facing response.

## Run & Operate

- `python -m real_estate_mvp.main` — mock-first local console
- `python -m real_estate_mvp.main --simulate` — 20-inquiry acceptance and reliability simulation
- `python -m pytest -q real_estate_mvp/tests` — Python tests
- `python -m real_estate_mvp.audit real_estate_mvp/sample_data/sample_whatsapp_export.txt` — offline synthetic chat audit
- `python -m real_estate_mvp.main --pending` — inspect persisted work after restart
- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- Live Telegram and Gemini environment values are optional in mock mode; see `real_estate_mvp/.env.example`

## Stack

- Python 3.11+, python-telegram-bot, Google GenAI SDK, tenacity, python-dotenv
- Local JSONL persistence; no database for ASTER
- The surrounding workspace also contains its original pnpm API and design scaffolds

## Where things live

- `real_estate_mvp/bot.py` — Telegram ingestion, acknowledgement, and review callbacks
- `real_estate_mvp/queue.py` — founder authorization, approval, rejection, and manual send
- `real_estate_mvp/knowledge.py` and `data/properties.json` — deterministic demo property data
- `real_estate_mvp/storage.py` — local inquiry JSONL and append-only event log
- `real_estate_mvp/audit.py` — offline WhatsApp-export audit only
- `real_estate_mvp/tests/` — unit and stress tests

## Architecture decisions

- No AI-generated buyer response is sent without an explicit founder action.
- ASTER uses JSONL rather than the template workspace database.
- Initial tests use synthetic inquiries and clearly labeled demo properties only.
- WhatsApp support is offline text-file parsing; there is no WhatsApp or Meta API.
- An ambiguous `sending` state is never resent automatically after restart.
- The founder must confirm `/resolve_send <id> delivered|not_delivered` after inspecting Telegram before retrying an ambiguous send.

## Product

The MVP records Telegram inquiries, matches them to explicit property facts, prepares a mock or Gemini draft, and queues it for founder approval. Unknown facts are escalated for manual handling. An offline tool measures response times from synthetic or anonymized WhatsApp exports.

## Safety rules

- Never introduce a database, WhatsApp API, automatic AI sales, or unaudited property claims into this MVP.
- Keep `.env` files and credentials out of source control.
- Do not use real customer data during initial testing.

## Gotchas

- Run Python commands from the workspace root with `python -m real_estate_mvp...`; this keeps package-local `queue.py` from shadowing Python's standard library module.
- The initial property catalog is synthetic. Review messages display a DEMO DATA warning.
- A delivery timeout can be ambiguous because Telegram has no send idempotency key; inspect before manually retrying a persisted failed send.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
- See `real_estate_mvp/README.md` for install, mock, audit, security, and live setup
