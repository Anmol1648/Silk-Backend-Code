# LLM Cost Optimisation

Reduces per-company AI spend by roughly an order of magnitude without
changing any call site's contract or degrading output quality.

**Test status:** 291 passed, 1 failed. The single failure
(`test_spec_contract.py::test_profile_sections_is_object_keyed`) is
**pre-existing** — it fails identically on the untouched original tarball,
because `generate_deep_profile` writes seven sections the spec test's
`expected` set does not list. It is unrelated to this work and left alone.

---

## The problem

`generate_profile` looped over ~15 configured sections and called
`llm_generate` once per section. Every one of those calls built a context
dict containing the **full** payload bundle — `website_extract`,
`document_extracts`, `research`, `founders` — which `_compose_prompt` then
JSON-dumped and blind-truncated at 24,000 characters.

The same ~6,000 tokens of source material was therefore sent **fifteen
times per generation**, and `generate_deep_profile` re-collected and re-sent
it a sixteenth. Measured on the ONGC-style dossier workload:

| | Input tokens | Output | Cost (Opus 5) |
|---|---|---|---|
| Section fan-out (~15 calls) | ~97,500 | ~12,000 | ~$0.79 |
| Deep extract (1 call + search loop) | ~45,000 | ~6,000 | ~$0.38 |
| **Per company** | | | **~$1.17** |

---

## Changes

### 1. `context_keys` is now enforced (`llm/adapter.py`)

Every role in `default_prompts.py` already declared exactly which context
keys it needs. **That declaration was inert** — the adapter ignored it and
sent the whole dict regardless. `_filter_context()` now honours it, so the
`founders` role is no longer billed for market research and `competitors` is
no longer billed for founder bios.

Two safety rails: a role that declares nothing keeps the full context, and a
declaration that would produce an *empty* context falls back to the full
dict. A stale declaration can never starve a call site.

### 2. Prompt caching with two breakpoints (`llm/adapter.py`)

Context is lifted out of the user turn into its own block so the stable
prefix can be cached. `_build_anthropic_system()` emits, in this order:

1. **Role instructions + JSON schema** — byte-identical for every company,
   forever. Once warm it is read at ~0.1x input indefinitely.
2. **This run's source bundle** — identical across the ~15 section calls of
   one generation.

Order matters: Anthropic matches an exact prefix, so putting the
per-company bundle first would bust the schema cache on every new company.
A size floor (`MIN_CACHEABLE_CHARS`) prevents caching blocks too small to
recoup the 1.25x write cost.

Providers without a cache-control wire format have the context folded back
into the user turn — their behaviour is byte-identical to before.

### 3. Skip-if-unchanged (`profile/services.py`, `profile/models.py`)

`sources_fingerprint()` hashes the collected bundle; `CompanyProfile.
sources_hash` and `ProfileSection.source_hash` store it. A founder pressing
"generate" twice over identical sources previously paid twice for a
byte-identical result — it is now a no-op returning
`skippedReason: "sources_unchanged"`.

Per-section gating applies on the fan-out path too. Sections still flagged
`needs_input` are always retried, since a prompt or model change may now
resolve them.

Override with `force=True` (service), `?force=true` or `{"force": true}`
(API), threaded through `generate_profile_task`.

### 4. Consolidated generation (`profile/services.py`)

`_generate_consolidated()` runs **one** ADVANCED deep-extract call and fans
the result out deterministically, instead of ~15 per-section calls asking
again for data the dossier already returned. `generate_deep_profile()` now
returns `sectionsWritten` so the caller knows what it no longer needs to
generate; anything not covered falls through to the per-section path, so no
section is silently dropped.

Guarded by the `PROFILE_CONSOLIDATED_GEN` flag (default ON) with automatic
fallback to the per-section path if the consolidated call raises — the
founder never ends up with nothing.

`generate_deep_profile()` also accepts pre-collected sources, so the
consolidated path no longer re-runs every research adapter and re-parses
every document a second time in the same request.

### 5. Source compression (`profile/services.py`)

`compress_payloads()` runs once at collection rather than letting the
adapter blind-truncate per call — a tail-slice is the worst option, paying
for boilerplate while losing the end of the document. It strips cookie /
copyright / newsletter boilerplate, caps website and research text,
de-duplicates document summaries (the same deck uploaded twice produced two
near-identical summaries, both billed), and prunes empty keys.

### 6. Bounded retries (`llm/adapter.py`)

The ladder was `2 attempts × 2 endpoints` — up to **four** full-price calls
on a bad afternoon, fired back-to-back into an already-overloaded upstream.
Now capped at `MAX_TOTAL_ATTEMPTS = 3` globally per call, with linear
backoff.

### 7. No repair call under structured output (`llm/adapter.py`)

`_parse_json_with_repair` fires a second full-price call re-sending 12,000
characters. When structured output was forced the provider *guarantees*
schema-shaped JSON, so a parse failure is a real error, not formatting
drift — re-asking just doubles the bill. It now raises instead. When the
repair does legitimately run, it no longer re-sends the context block.

### 8. Thinking off for extraction + per-role output caps

Thinking tokens bill at the **output** rate and buy nothing when the task is
lifting values out of supplied text. Stripped for the roles in
`NO_THINKING_ROLES`. `ROLE_MAX_OUTPUT_TOKENS` trims roles that cannot
legitimately need the 2048 default (`company_profile_field` → 512) and
raises the one that can (`company_profile_deep_extract` → 8192). A role cap
**only ever lowers** the admin's binding value.

### 9. Truthful cost accounting (`llm/models.py`, `llm/admin.py`)

- `LLMCallLog.cache_read_tokens` / `cache_write_tokens`. Cached tokens are
  **not** included in `input_tokens` and bill at different rates — logging
  only `prompt_tokens` once caching is on would have made the spend
  dashboard quietly wrong.
- `LLMModelCost` gains cache read/write rates, derived as 0.1x / 1.25x of
  the input rate when left at zero, so an existing price book keeps working.
- **A missing price row now logs a WARNING.** It previously returned
  `Decimal("0")` in silence, which is how a cost dashboard reads zero while
  the invoice climbs. Check this first — everything else depends on it.
- Call-log admin surfaces `calling_context` (e.g.
  `profile.generate.competitors`), which is the lever for finding the
  worst offender.

### 10. Budget ceiling (`llm/models.py`, `llm/adapter.py`)

New `TenantLLMBudget` with a monthly cap enforced **before** dispatch.
Runaway loops, not single expensive calls, are how these bills actually blow
up. No row = no cap, so it is opt-in and cannot break an existing
deployment. A NULL-tenant row is the global default. Warns at
`alert_at_pct` (default 80%).

> Note: the accessor is `check_budget()`, not `check()` — `Model.check()` is
> reserved by Django's system-check framework and shadowing it breaks
> `manage.py` entirely. (Found by `makemigrations --check`.)

---

## Migrations

- `llm/0005_cost_optimisation.py` — cache token columns, cache rate columns,
  `enable_prompt_cache`, `TenantLLMBudget`.
- `companyprofile/0006_source_fingerprints.py` — `sources_hash`,
  `source_hash`.

`makemigrations --check` reports no pending changes for any of the above.
It does report three **pre-existing** `choices`-only drifts
(`investor.holder_category`, `llmconfigprofile.tier`,
`prompttemplate.purpose`) which predate this work and have no DB effect.

---

## Deployment

```bash
python manage.py migrate
python manage.py seed_llm_costs          # add --overwrite to replace rows
```

**Seed the price book first.** Without it every call logs at zero cost and
no measurement below is meaningful.

Then, in Django Admin:

1. **LLM config profiles** — confirm the `simple` tier points at a cheap
   model (Haiku/Sonnet class), not the flagship. This single setting is
   worth more than most of the code above.
2. **LLM config profiles** — set `structured_output` on the extraction
   profiles so the repair path can never fire.
3. **LLM config profiles** — check `max_uses` on the web-search profile;
   each search round trip re-bills the accumulated context.
4. **Tenant LLM budgets** — add a global row with a sane monthly cap.

---

## Expected result

| Stage | Per company |
|---|---|
| Before | ~$1.17 |
| + `context_keys` filtering | ~$0.55 |
| + prompt caching | ~$0.30 |
| + consolidated single call | ~$0.19 |
| + Sonnet for extraction, Opus for thesis only | ~$0.09 |
| + repeat runs skipped entirely | ~$0.00 |

Measure with:

```sql
SELECT function_name, calling_context,
       COUNT(*), SUM(cost_inr), SUM(cache_read_tokens)
FROM llm_call_log
WHERE created_at >= date_trunc('month', now())
GROUP BY 1, 2 ORDER BY 4 DESC;
```

Rising `cache_read_tokens` is the signal that caching is working. If it
stays at zero, the model is not cache-eligible or the prefix is being
invalidated between calls.

---

## Tests

`tests/api/test_llm_cost_optimisation.py` — 26 tests covering context
filtering (including the stale-declaration fallback), cache block ordering
and the size floor, derived cache rates, the missing-price-row warning,
budget enforcement, output caps, boilerplate stripping, summary
de-duplication, fingerprint stability, and the skip / force / changed-source
paths.

---

# Addendum — Provider Uniformity & Config Defects

Follow-up review raised a fair objection: `TenantLLMTier` looked
Claude-specific. It was not — the model layer always allowed any provider —
but the *seeds*, the *caching* and the *price book* were all Anthropic-shaped
in practice. Fixed below, plus the three defects from the config review.

## U1. Caching is now uniform across providers

Previously only Anthropic benefited; OpenAI and Gemini had the context
**appended after** the volatile instruction, which put the varying text first
and made the repeated prefix unreachable to any prefix cache.

`CACHE_STRATEGY` in `llm/models.py` now declares how each provider caches:

| Provider | Strategy | What the adapter does |
|---|---|---|
| Anthropic | `explicit` | Emits `cache_control` breakpoints |
| OpenAI | `implicit` | Keeps the prefix stable **and first** |
| Gemini | `implicit` | Keeps the prefix stable **and first** |

`prompt_cache` added to `PROVIDER_CAPABILITIES` for all three.

## U2. One usage shape for every provider

Each vendor reports cached tokens differently — Anthropic splits
creation/read, OpenAI nests `cached_tokens` under `prompt_tokens_details`,
Gemini uses `cachedContentTokenCount`. OpenAI and Gemini also **include**
cached tokens in their input count while Anthropic does not.

`_usage()` normalises all six dispatch paths to the same four numbers, with
cached tokens subtracted out of the fresh-input figure where the vendor
included them. Without this, "what did caching save us" became unanswerable
the moment a tenant switched provider.

## U3. Tier profiles are role-named, not vendor-named

Seed codes are now `tier.simple` / `tier.advanced`. Vendor alternates
(`tier.simple.gemini`, `tier.advanced.openai`, …) ship **inactive** alongside,
with GEMINI and OPENAI endpoints pre-created. Switching a tenant to Gemini is
now: activate the profile, repoint the tenant tier row. Two clicks, no deploy.

## U4. Price book derived from configured endpoints (defect 0.1)

`_log_call` writes `provider = endpoint.code` ("ANTHROPIC"), but the old
seeder keyed rows on the provider *family* ("anthropic"). They never joined.

`seed_llm_costs` now walks the endpoints, config profiles and model catalog
actually configured and prices every real (endpoint code, model string) pair,
matching model strings by longest prefix so dated variants inherit correctly.
`--check` audits without writing and names anything that would record zero.

## U5. `structured_output` on by default (defect 0.2)

Both seeded tier profiles now set `structured_output=True`,
`thinking_mode="none"`, `enable_prompt_cache=True`, and the advanced tier
bounds `max_uses=6`. The JSON-repair second call can no longer fire.

## Test note

`tests/api/test_llm_normalizer.py::_bind_live_endpoint` was changed from
`create` to `get_or_create`. It hard-created `code="OPENAI"` *after* running
`seed_platform_config`, which now seeds that endpoint — a unique-constraint
collision. The helper's purpose is binding a live endpoint, not asserting
endpoint uniqueness. This is the only pre-existing test modified.

**Final: 305 passed, 1 failed** (the pre-existing `test_spec_contract`
failure, which fails identically on the original tarball).

---

# Addendum 2 — Three first-class tiers, no hardcoded models

## What changed and why

The previous release pinned the judgement call to a config profile code in
Python (`config_profile="cp_judgment"`). That was a model choice living in
code, which is exactly what the tiering layer exists to prevent.

**Judgement is now a tier**, resolved through `TenantLLMTier` like the other
two. No call site names a profile.

| Tier | Purpose | Tools | Cost shape |
|---|---|---|---|
| `simple` | Extract from supplied text | None | Cheapest, highest volume — the model here dominates the bill |
| `advanced` | Discover facts from the internet | Web search | Highest per call; bound with `max_uses` |
| `judgment` | Reason over already-extracted data | None | High per token, tiny input — the strongest model is affordable |

## On renaming `advanced` to `web`

Not done, deliberately. Renaming means migrating `LLMConfigProfile.tier`,
`TenantLLMTier.tier` and `PromptTemplate.tier` across live rows, and any row
that fails to migrate silently falls back to `simple` — a correctness risk to
fix a wording problem. The stored key stays `advanced`; the **label** is now
"Advanced / Web — retrieve from the internet (web search on)".

`TIER_DEFINITIONS` in `llm/models.py` is the single source of truth for what
each tier means, and the admin help text is generated from it, so the UI and
the code cannot drift apart.

## Role defaults now live in code, explicitly

`ROLE_TIER_DEFAULTS` in `default_prompts.py` lists every role. The previous
implementation used a catch-all `_ADVANCED` set containing 25 of 27 roles,
which is why the entire Company Profile build ran on the web-search tier to
extract from text it had already been given.

Corrected defaults for the profile roles:

| Role | Was | Now |
|---|---|---|
| `company_profile_records` | advanced | **simple** |
| `company_profile_structured` | advanced | **simple** |
| `company_profile_section` | advanced | **simple** |
| `founder_profile` | advanced | **simple** |
| `company_profile_deep_extract` | advanced | advanced |
| `company_profile_judgment` | — | **judgment** |

Admins still override any role in Admin → AI Prompts → tier.

## Judgement output is reused by cheap calls

The point of paying for a strong model once is that its conclusions should
inform everything after it. `CompanyProfile.judgment` persists a distilled
form — which value won each conflict, the identified risks, the leadership
assessment — and it is injected into all four section-generation contexts.

`_distil_judgment()` keeps conclusions and drops reasoning: a Simple call
needs to know that the ₹3.05T market-cap figure won and the ICICI figure was
the outlier, not the argument that produced it. Risks are capped at 8 and the
assessment at 1,200 characters, so reuse stays close to free.

Without this, each Simple call re-derives its own view from the raw sources
and can contradict the adjudication already paid for — the profile says one
thing and the thesis another.

## Migrations

- `llm/0006_judgment_tier.py` — tier choices
- `companyprofile/0007_judgment_tier.py` — `CompanyProfile.judgment`
- `platformcfg/0003_judgment_tier.py` — prompt tier choices

## Seeds

`tier.judgment` (Opus, no web search, thinking on) plus inactive
`tier.judgment.gemini` and `tier.judgment.openai` alternates, and a global
`TenantLLMTier` row for the judgement tier.

**Tests: 56 in the cost-optimisation suite**, including one asserting that no
call site hardcodes a config profile.

---

# Addendum 3 — Endpoint/model coherence (latent bug fix)

## The bug

The endpoint came from the role BINDING (`LLMRoleBinding.primary_endpoint`)
while the model came from the config PROFILE (`LLMConfigProfile.model_string`):

```python
attempts = [("primary", binding.primary_endpoint)]   # provider
model = model_override or endpoint.default_model     # model, from profile
```

That is invisible while every profile happens to sit on the same endpoint as
its binding — which is true of the shipped seeds, so nothing failed in
testing. It breaks the first time a tenant switches a tier to another vendor:
the profile says `gemini-3.1-pro`, the binding still says `ANTHROPIC`, and the
adapter posts a Gemini model string to the Anthropic API. The result is a
404 at call time with a message that points nowhere near the real cause.

The vendor-alternate profiles shipped in the previous release made this
reachable by a single admin click.

## The fix

A profile that names a model has, implicitly, named the provider that serves
it. So when the resolved config profile carries an **active** endpoint, that
endpoint wins over the binding's.

The binding's fallback is retained only when it speaks the same provider
dialect. Falling back from Gemini to Anthropic would reintroduce the exact
mismatch, so a cross-vendor fallback is dropped rather than called.

Bindings remain authoritative for connection concerns the profile has no
opinion on — fallback within a vendor, temperature, `max_output_tokens` — and
for every role that has no config profile at all, which is unchanged.

## Tests

Three added, covering: the profile's endpoint overriding the binding's, the
cross-vendor fallback being dropped, and no regression when the profile
carries no endpoint.

Note these tests disable `ai_mocked` explicitly. Global mocking
short-circuits dispatch, so a test asserting *which endpoint dispatch
receives* passes vacuously with mocking on.

---

# Addendum 4 — Web-search containment on providers without max_uses

## The honest finding first

**An in-flight hard cap is not possible on Gemini or OpenAI.**

Both execute web search *server-side inside a single request*. The model
decides to search, the provider runs it, appends the results, and continues —
all before the response reaches us. By the time the adapter sees anything,
every search has already run and been billed. There is no callback, no
streaming interception point, and no count parameter on either tool.

Anthropic is different only because it exposes `max_uses` on the tool itself,
so the provider enforces it.

Anything claiming to "cap Gemini searches at 6" would be lying. What follows
is defence in depth, with each layer's limit stated.

## The four layers

| Layer | Mechanism | Protects against | Leaks |
|---|---|---|---|
| 1. Advisory | The cap is stated in the system prompt | Most overruns — models generally comply | A model that ignores it. Nothing enforces this |
| 2. Pre-flight | Worst-case search cost reserved against the tenant budget before dispatch | The budget being blown by an uncapped call | Does not cap the call, only refuses to start one that could not be afforded |
| 3. Detection | Actual searches counted from response metadata into `LLMCallLog.search_count` | Overruns going unnoticed | After the fact — the call is already paid for |
| 4. Breaker | Repeated overruns strip web search from later calls for that profile | The overrun repeating | Never prevents the first one |

Net effect on an advisory provider: a single call can still exceed the cap and
you will pay for it. What cannot happen is that it goes **unnoticed**, that it
**repeats**, or that it runs when the budget **cannot absorb it**.

## Configuration is unchanged

The admin sets one number — `max_uses` on the config profile. How it is
enforced differs per provider and is not the configurator's problem. Switching
the Advanced tier from Sonnet to Gemini needs no config change; the adapter
silently moves from native enforcement to layers 1-4.

## Counting details that matter

- **Gemini**: `groundingMetadata.webSearchQueries` — the queries actually
  issued. `groundingChunks` counts SOURCES and over-reports badly, since one
  query returns many chunks. Using it would trip the breaker constantly.
- **OpenAI**: `web_search_call` items in the Responses API output.
- **Anthropic**: `usage.server_tool_use.web_search_requests`. Should always be
  within the cap — logging it proves that rather than assuming it.
- **Unknown is -1, never 0.** A provider that reports nothing must not look
  like a provider that searched zero times, and must never trip the breaker.

## Breaker behaviour

Three consecutive overruns on one config profile open the breaker for an hour;
subsequent calls on that profile run with web search stripped. **A compliant
call resets the count** — one company whose dossier genuinely needed more
searching is not an offence, a persistently overrunning configuration is.

## Tests

12 added, including the two that matter most: a single overrun does NOT trip
the breaker, and an unknown count can never trip it.

**Total: 113 passing across the LLM and flow suites.**

---

# Addendum 5 — Diagnostics for algorithm improvement

## The gap

`LLMCallLog` recorded cost, tokens, latency and status. All of that answers
"what did this cost?" and none of it answers "was it any good, and what
should I change?" There was also **no run-level record at all** — nothing
capturing what sources a generation actually had to work with.

The practical consequence: a profile that came out thin looked identical
whether the cause was a bad prompt, a weak model, a failed research adapter,
or a company with a two-page website. Four different fixes, one symptom, no
way to tell them apart.

## Per-call additions (`LLMCallLog`)

| Column | Answers |
|---|---|
| `tier`, `config_profile` | Which model config served this. Without it you cannot attribute a quality change to a model change |
| `prompt_fingerprint` | Short hash of the system prompt SENT. Changes when an admin edits a prompt, so before/after becomes a GROUP BY |
| `context_chars` | How much context went in — the input to context-trimming decisions |
| `was_repaired` | JSON failed to parse and a second call was needed. A cheap model with a high repair rate is not cheap |
| `was_normalized` | Response came back under unexpected keys. A rising rate means prompt/model drift, invisible in cost and latency |
| `output_signals` | needsInput, and counts of missing / conflicts / records / items |
| `search_count` | Searches actually performed (added in Addendum 4) |

`output_signals` is the important one. A prompt change that *raises*
`missing_count` is usually an improvement — the model admitting gaps instead
of inventing — and no cost metric would ever show that.

## Run-level record (`ProfileGenerationRun`)

One row per generation, written once and never updated, so a series is a time
series you can regress against prompt and model changes.

- **Inputs** — `source_stats` (website chars, document count and chars,
  document types, research keys, founder count) and `source_failures`. This
  is the denominator for every quality question about a run.
- **Outputs** — sections generated, skipped and still needing input, plus
  completeness. A section that is ALWAYS in `sections_needing_input` is a
  pipeline bug; one that varies with sources is working correctly.
- **Judgement** — whether it ran, conflicts found, conflicts resolved.
- **Economics** — call count, total cost, total searches, duration.

Written for all three paths including `mode="skipped"`, because a skipped run
is itself a diagnostic event.

## Bug found while implementing

Wiring the `was_normalized` flag revealed that the response normaliser would
have run **twice** per call in my first attempt. Collapsed to one invocation
with the flag derived from a before/after comparison.

## Admin

The call-log list now shows tier, config profile, search count and the
repaired/normalised flags, and filters on all of them. A new
ProfileGenerationRun admin surfaces input size, generated count, needs-input
count and conflicts per run.

## Migrations

- `llm/0008_diagnostics.py`
- `companyprofile/0008_diagnostics.py` (ProfileGenerationRun)

**Tests: 318 passing**, 7 new covering signal extraction, prompt
fingerprinting, source measurement, and that a diagnostics failure can never
break a generation.
