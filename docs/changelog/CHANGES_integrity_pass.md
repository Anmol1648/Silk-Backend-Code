# Configuration integrity pass — backend v15

Fixes the two faults seen on the UAT server, plus every other instance of the
same two patterns found by auditing the codebase.

## Deploy

```bash
pip install -r requirements.txt
python manage.py migrate                  # includes llm/0009 (de-duplicates)
python manage.py seed_platform_config
python manage.py diagnose_config
```

`migrate` must run BEFORE seeding: `llm/0009_dedupe_global_tier_rows` removes
the duplicate rows that made the seeder crash.

---

## 1. Seeding crashed: `TenantLLMTier.MultipleObjectsReturned`

**Root cause.** `TenantLLMTier` had `UniqueConstraint(["tenant_id", "tier"])`,
and `tenant_id` is nullable. PostgreSQL treats NULLs as DISTINCT in a unique
index, so the constraint never applied to the GLOBAL rows (`tenant_id IS
NULL`) — exactly the rows the seeder creates. Repeated seeding produced
duplicates; `get_or_create` then called `.get()`, which raised.

**Three separate fixes, because the bug had three separate failure modes:**

1. *The duplicates exist* → migration `llm/0009` de-duplicates, keeping the
   most recently updated row of each group.
2. *They could be created at all* → partial unique constraints added for the
   NULL case on `TenantLLMTier` and `TenantLLMBudget`. Verified: attempting to
   insert a second global row now raises `IntegrityError`.
3. *One failure destroyed the whole run* → `seed_platform_config` was a single
   `@transaction.atomic` over all ten blocks, so the crash in the LAST block
   rolled back the nine that had succeeded. Each block is now its own
   transaction; a failure is reported with its remedy, the remaining blocks
   still run, and the command exits non-zero so a deploy script still notices.

`_get_or_create_unique` is also used for the global rows, so seeding recovers
from a duplicated database rather than crashing on it.

## 2. Five LLM roles were called but never declared

`LLM_ROLES` is the choice list for `LLMRoleBinding.role`. A role missing from
it **cannot be bound in the admin console**, and `llm_generate` raises
`LLMUnavailable` when a role has no binding. These five were called in code and
absent from the list, so each failed at runtime on any system with mocked AI
switched off — with no way for an administrator to fix it:

| Role | Called from |
|---|---|
| `company_profile_judgment` | `profile/services.py` — adjudicates conflicting figures |
| `assessment_extraction` | `assessment/extraction.py` |
| `assessment_rubric_review` | `assessment/tasks.py` |
| `investor_classification` | `investors/classify.py` |
| `investor_identity` | `investors/identity.py` |

All five declared; migration `llm/0010` updates the field choices.

## 3. The diagnostic reported a fault that was not there

`diagnose_config` and `preflight_report` queried role `profile_deep_extract`.
The real name is `company_profile_deep_extract`, so the query matched nothing
and the check reported "no endpoint bound to the deep-extract role"
**unconditionally** — whether or not a binding existed. A diagnostic that
cannot pass is worse than no diagnostic, because it sends the reader after a
problem they may not have. Corrected in both places.

## 4. Guarding the class, not just the instances

Both faults are of a kind that normal tests miss: the code runs fine until the
exact line executes. `tests/api/test_configuration_integrity.py` scans the
source rather than exercising behaviour.

- `test_every_called_role_is_declared` parses every `llm_generate(role=...)`
  call in the codebase and fails if the role is not in `LLM_ROLES`. This is
  what would have caught all five above.
- `test_diagnostics_reference_real_role_names` fails if diagnostic code names a
  role that does not exist.
- `test_no_unguarded_nullable_unique_constraint` walks every model and fails on
  any unique constraint containing a nullable column without a partial or
  expression constraint covering the NULL case.
- `test_global_tier_rows_cannot_be_duplicated` asserts the database now
  rejects the duplicate.
- `test_a_failing_block_does_not_prevent_the_others` simulates a failing seed
  block and asserts the earlier blocks survive.
- `test_registry_reaches_the_expected_size` asserts 23 sections.

## Audit findings that were NOT problems

For completeness: the audit also checked every research-source key referenced
in code against the adapter registry (all valid), and every `get_or_create`
call for the nullable-key pattern (no others). Runtime tier and budget
resolution uses `filter().first()`, so it was never exposed to the duplicate
rows — only the seeder was.

## Tests

562 tests pass.

---

# v16 — checks that would have caught the UAT findings

Two conditions on the UAT server passed every check and would still have
produced a failing or lossy system. Both are now checked.

## 1. Research model chain (new CRITICAL check)

`diagnose_config` confirmed a role binding existed and reported the ENDPOINT's
default model. But the model actually used comes from the config profile for
the role's tier, not from the binding. On the server the advanced profile was
set to `gemini-2.0-flash` — a model absent from the catalog entirely (the
oldest listed is 2.5, already marked retiring). The research call would have
404'd at runtime.

The new check resolves the whole chain — role → tier → config profile → model
→ endpoint — and fails on:

- the deep-extract role not running on the ADVANCED tier (a prompt tier
  override silently removes web search);
- no config profile resolving, or the profile being inactive;
- web search OFF on the advanced profile;
- the profile's endpoint not existing;
- the profile's provider not matching its endpoint's provider (a Gemini model
  string sent to an Anthropic API);
- a model string absent from the catalog — the remedy names the models
  actually available for that provider;
- a model marked retiring (WARN, not FAIL).

## 2. File storage (check corrected, severity raised to CRITICAL)

The previous check looked for `MEDIA_ROOT`, which this project does not use,
so it reported "not configured" regardless of the real setting — and hid a
data-loss risk underneath. It now reads `FUNDOS_STORAGE_BACKEND`,
`FUNDOS_STORAGE_LOCAL_ROOT` and `FUNDOS_STORAGE_BUCKET`.

It also detects **ephemeral paths**. On the server, local storage resolved to
`/d01/fundos/releases/<timestamp>/…/var/storage` — inside the release
directory. Documents written there are orphaned by the next deployment. Any
path containing `/releases/`, `/current/` or `/versions/` now FAILS with an
explicit warning about orphaning. The same test applies to the diagnostic log
directory (WARN there — history is lost, not customer data).

## Tests

`ResearchModelChainTests` and `EphemeralPathTests` pin every branch above,
using the exact values seen on the server. 572 tests pass.

---

# v17 — the seed configures the system, instead of documenting how to

Three of the setup steps were manual, and each was a step where a mistake
produced an empty profile rather than an error. They are now derived.

## 1. Data paths default somewhere durable

`FUNDOS_STORAGE_LOCAL_ROOT` and `LOG_DIR` both defaulted under `BASE_DIR` —
the CODE directory. Under a release-per-deploy layout that directory is
replaced on every deployment, so uploaded documents and diagnostic logs were
defaulted into a folder the next release would orphan. This is data loss
waiting on a deploy, not untidiness.

`_durable_data_dir()` now derives a path that survives:

| Code path | Data path |
|---|---|
| `/d01/fundos/releases/2026-08-10-2130/FUNDOS/fundos-backend` | `/d01/fundos/shared/data` |
| `/srv/app/versions/17/backend` | `/srv/app/shared/data` |
| `/home/dev/fundos-backend` | `/home/dev/fundos-backend/var` (unchanged) |

Override with `FUNDOS_DATA_DIR`, or the individual variables as before. No
environment editing is required for the release layout to be safe.

## 2. Provider selection is derived from which key is set

An API key being present in the environment is the administrator stating which
provider they intend to use. Seeding now reads that:

- an endpoint whose key variable is set is ACTIVATED (they were seeded
  inactive, so setting a key alone did nothing);
- the three tier profiles are pointed at that provider with current models;
- the global tier defaults are repointed;
- other providers' profiles for those tiers are deactivated, so exactly one is
  selectable.

**It converges, it does not overwrite.** A configuration that can work is left
untouched. A configuration that provably cannot — a model absent from the
catalog, web search off on the advanced tier, an endpoint with no key — is
repaired, and the repair is reported. Verified both ways by test.

## 3. Every role is bound automatically

A role with no binding raises `LLMUnavailable` at call time; it does not fall
back. Binding roles by hand was the most frequently missed step in setup, and
its symptom — an empty profile — points nowhere near its cause. Seeding now
binds every role in `LLM_ROLES` to the usable endpoint, creating only what is
missing and repointing only bindings whose endpoint cannot serve a call.

With no key set anywhere, seeding does NOT invent a binding: a binding that
looks configured and fails at call time is worse than none.

## 4. Seeded models are current

The Gemini tier profiles were seeded with `gemini-2.5-flash` and
`gemini-2.5-pro`, both carrying a published retirement date of 16 October
2026. Now `gemini-3.1-flash-lite`, `gemini-3.6-flash` and `gemini-3.1-pro`. A
test asserts no seeded model is one the catalog marks as retiring.

## Result

On a machine with `GEMINI_API_KEY` set, a single `seed_platform_config` run
produces:

```
llm endpoint: activated GEMINI (its API key is set)
llm provider selection: gemini via GEMINI (simple=gemini-3.1-flash-lite,
                        advanced=gemini-3.6-flash, judgment=gemini-3.1-pro)
llm role bindings: 32 created, 0 repointed, 32 total (endpoint GEMINI)
```

and `diagnose_config` reports the research chain as PASS. Parts 2, 3 and 4 of
the Company Profile configuration reference become verification rather than
data entry.

## Tests

581 pass. `SelfConfiguringSeedTests` and `DurableDataPathTests` cover both
directions: broken configuration is repaired, valid choices are preserved.
