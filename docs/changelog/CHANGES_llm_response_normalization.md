# LLM "success but empty" fix — response normalization (Aug 2026)

Addresses LLM_ISSUE2: connectivity is fixed (calls log Success) but AI
generation produces no content.

## Root cause

"Success" in the Call Log only means the provider returned valid JSON — it
does NOT mean the JSON had the keys the profile code reads. The four Company
Profile roles have no strict schema, so a valid-but-differently-shaped
response passes validation and is then read as empty downstream, saving the
section blank while the call logs success. Examples of drift that broke it:

* narrative sections read `content`/`summary`; a model returning prose under
  `text` / `body` / `narrative` → blank section.
* record sections read `records` (list) or `structured.items`; a model
  returning `{"key_people": [...]}`, `{"competition": [...]}`, `{"items": ...}`
  or a bare list → nothing stored.
* financial_summary reads `structured.items` / `structured.observations`;
  top-level `financials` / `observations` → nothing stored.

## Fix (code-side, no prompt change, no migration)

New `fundos/llm/response_normalizer.py` coerces whatever reasonable JSON a
model returns into the canonical shape each role's consumer expects. It runs
in the adapter right after JSON parse/repair and before validation, so it
covers both primary and fallback endpoints uniformly, for the live path.

It is conservative: a canonical key that is already present and non-empty is
left untouched; alternates are only used to FILL a missing/empty canonical
key. No numbers are invented — values the model already returned are just
moved into the expected slot. It is section-aware for the profile roles
(e.g. `{"products": [...]}` → items for products_services; flat
business_model fields → `object`). On anything unexpected it returns the
input unchanged, so it can only help, never break a call.

Defense in depth: the narrative consumer (`_generate_section`) also does a
second-pass extraction across common prose keys.

## Bonus bug fixed

`fundos/llm/adapter.py` had a redundant local `import LLMUnavailable` inside
`llm_generate` that shadowed the module-level import, so the final
`raise LLMUnavailable(...)` (when all endpoints fail) raised
"cannot access local variable 'LLMUnavailable'" instead of the real error.
Removed the shadowing import.

## Tests

`tests/api/test_llm_normalizer.py` runs generation in LIVE mode with a patched
dispatcher that returns off-shape JSON, and asserts sections still populate:
* list recovered from an aliased key (products / competition),
* narrative prose recovered from `text` instead of `content`.
Full suite green: 234 tests pass.

## Note

This makes the backend tolerant of shape drift. Re-seeding prompts
(`seed_platform_config`) is still recommended so prompts pin the canonical
JSON, but generation no longer depends on the model getting the exact key
names right.
