---
name: ASTER inquiry safety requirements
description: Durable product constraints for ASTER property inquiry drafting and founder review.
---

- Keep founder-written `/send` available when AI drafting fails, but never approve or send a failed AI draft as an AI-generated reply. Founder approval remains mandatory for AI-generated buyer replies.
- Answer only when property details are available, reliable, and supported by the database. Escalate missing, conflicting, unverified, or out-of-scope details; never invent prices, availability, payment terms, amenities, or other property facts.
- Demo listings are not verified real properties. Preserve their demo status wherever they are used or shown.
- Keep the intended outcomes distinct: `READY_FOR_APPROVAL`, `NEEDS_HUMAN_REVIEW`, and `DRAFT_FAILED`.

**Why:** The user explicitly stated ASTER's intended behavior and safety boundaries; diagnostic work must not silently change them.

**How to apply:** Follow these constraints when changing ASTER matching, context handling, draft generation, escalation, or founder review.
