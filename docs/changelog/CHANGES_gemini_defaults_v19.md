# v19 — Gemini works without configuration

One theme: every setting the system needs in order to function is now stated
by the system itself. v18 made these settings *possible* and *visible*; it
still required an administrator to know what to set. Each item below was a
FAIL or a WARN on the live install whose remedy began "Admin → …", which is
another way of saying the default was wrong.

---

## 1. The two CRITICAL failures: `simple` and `judgment` resolved to nothing

`diagnose_config` reported `Global tier rows [PASS] one global row for each of
the three tiers` alongside `Simple tier [FAIL] no config profile resolves`.
Both were correct. `_duplicate_tiers` counted rows without regard to
`is_active`; `TenantLLMTier.resolve_code()` filters on it. A row that exists
but is switched off resolves to `None` exactly as a missing row does, while
still appearing in the admin list — which reads as "configured".

`llm_generate` raises `LLMUnavailable` for a role whose tier does not
resolve. It does not degrade. So seven of the nine profile roles were
raising, and the two that worked were the ones on the advanced tier.

**Why seeding never fixed it.** `_llm_provider_selection` assessed the
**advanced** tier alone and returned early when it was healthy, on the
assumption that the three tiers are configured together. They are not: an
administrator repairing the tier that visibly failed leaves the other two as
they were, and every subsequent seed run then reported "left as-is".

Fixed — each tier is assessed and repaired independently, and a global tier
row that is switched off is switched back on. The `Global tier rows` check
now reports an inactive row as a FAIL naming the tier.

**This is also why the v18 thinking fix did not fully take.** That fix lived
in `capabilities_payload()`. With no config profile resolving there are no
capabilities, so nothing was sent and Gemini reverted to its default —
thinking was off on `advanced` and on everywhere else.

---

## 2. Thinking off is now a property of the adapter, not of the configuration

`_run_gemini` treats an absent thinking capability as `thinkingBudget: 0`,
exactly as absence already means "off" on Anthropic and OpenAI.

This is the difference between v18 and v19. v18 could only express "off"
through a config profile, so a call that reached the adapter without one —
an unresolved tier, an explicit `model_override`, a direct adapter call, a
role with no tier — still handed the decision to Gemini, whose answer is
"think", with the thoughts billed against `maxOutputTokens` and excluded from
`candidatesTokenCount`.

Thinking is now opt-in: state a positive budget on the config profile to
enable it, or `-1` for Gemini's dynamic budget, which is passed through as
given rather than turned into a zero. Anthropic and OpenAI are unchanged.

---

## 3. The output ceiling: "nobody chose this" is now distinguishable

`LLMRoleBinding.max_output_tokens` was `IntegerField(default=2048)` and
`_dispatch` computes `min(binding, role_cap)` — the binding can only ever
LOWER the ceiling. Because every row carried the shipped default, the value
2048 meant both "an administrator capped this role" and "nobody has ever
touched this", and the code had no way to tell them apart. Every fresh
install therefore ran `company_profile_deep_extract` at 2048 of its designed
8192 and returned a dossier cut off at the ceiling — parsed, repaired, and
logged as a successful call.

The field is now `null=True, default=None`. Blank means "use the budget this
role was designed for"; a number still only ever caps the role below it.
Migration `llm.0011` clears rows still holding exactly the old default, so
they fall back to the designed budget. **A value an administrator actually
chose is any other number and is left alone** — if 2048 was deliberate for
some role, re-enter it after migrating.

`DEFAULT_MAX_OUTPUT_TOKENS = 2048` is the fallback for roles with no entry in
`ROLE_MAX_OUTPUT_TOKENS`.

---

## 4. Capability defaults a tier needs in order to work

Filled in on every seed run, including on tiers that are otherwise healthy,
because these are properties of the provider's API rather than preferences.
Only UNSET values are filled, so a deliberate choice survives.

| Setting | Tier | Why it is not a preference |
|---|---|---|
| `max_uses = 6` | advanced | Unset means unbounded, and Gemini enforces the cap only by stating it in the prompt — so nothing was stated at all. |
| `user_location = {"country": "IN"}` | advanced | Search grounding without a locale returns US-weighted results for Indian companies. |
| `structured_output = False` | advanced (Gemini) | Gemini **rejects** `google_search` combined with a `responseSchema`. The searching tier's whole purpose is search, so the schema gives way; the response normalizer already copes without one. This combination fails the call outright. |
| `thinking_mode = tokens`, `thinking_budget = 4000` | judgment | The vendor alternate profiles were seeded without ever naming a mode, so they inherited the field default `"none"`. Since absence now means OFF on every provider, the tier whose entire purpose is reasoning was doing none. OpenAI gets `effort = medium`. |

---

## 5. Two endpoints for one provider no longer compete

An administrator who creates the endpoint by hand codes it whatever they
like — `GEMINI LLM` on the live install. Seeding by code then adds a *second*
row for the same vendor. Both read the same environment variable, so both
look usable, and which one wins is arbitrary; because role bindings and
config profiles reference an endpoint **by code**, the system can end up
split across the pair.

`_usable_endpoints` now selects one endpoint per provider, preferring the row
the role bindings already point at, then active, then priority. The canonical
row is still created — the vendor alternate profiles reference it by code —
but it is never selected while the administrator's row exists. Redundant rows
are named in the seed output.

---

## 6. `provider_kind` is not `provider`

An endpoint's `provider_kind` is a wire protocol (`openai_chat`,
`deepinfra`); a config profile's `provider` is a capability family
(`openai`). `_llm_provider_selection` used the kind directly, which on an
OpenAI deployment created profiles coded `tier.judgment.openai_chat`, with a
provider value outside `CONFIG_PROVIDERS` and — because `_RECOMMENDED` is
keyed by provider — an **empty model string**. The existing
`ENDPOINT_KIND_TO_PROVIDER` map is now applied. Latent; found while testing
the above.

Also: the seeded endpoint default models were `gemini-2.5-flash` (published
retirement date) and `gpt-4o`. Now `gemini-3.6-flash` and `gpt-5.6-sol`.

---

## 7. Diagnostics

- **Diagnostic log** verifies the `fundos.generation` logger actually has a
  file handler attached, rather than inferring it from the file's existence —
  the v18 failure left the file in place while dropping the handler. An empty
  file now reads *"handler attached, empty (nothing generated since the last
  restart)"* instead of `0 KB`, which read as a fault and was not one.
- **Global tier rows** reports an inactive global row as a FAIL (§1).
- **Output token ceilings** remedy now says to *clear* the binding value
  rather than raise it, matching §3.

---

## Tests

`tests/api/test_gemini_thinking.py` — 22 tests (11 new):

- absence of a thinking capability sends `thinkingBudget: 0`; `-1` passes
  through; Anthropic and OpenAI semantics unchanged
- the binding ceiling field is nullable with no default; dispatch falls back
  to the role cap; a stated value still only lowers
- `SeedDefaultsTests` — every tier resolves after a fresh seed; a healthy
  advanced tier does not mask the other two; the searching tier is bounded;
  Gemini search is never combined with a response schema; the judgement tier
  actually reasons; bindings do not cap the dossier
- `EndpointIdentityTests` — a hand-coded endpoint is not duplicated, and the
  endpoint already in use wins

`tests/api/test_llm_cost_optimisation.py` — `ShippedDefaultsMixin` added to
the four classes that assert the configuration a fresh install ships with.

**The six failures documented in v18 as "pre-existing, unrelated" are fixed,
and were never unrelated.** They failed whenever `GEMINI_API_KEY` was present
in the shell running the suite, because seeding then correctly auto-selected
Gemini while those tests asserted the shipped Anthropic defaults by name. A
test describing the shipped state must control the input that decides it, so
the mixin clears the provider key variables before seeding. That is a defect
in the tests, not in the seeder — but it meant the suite could not be trusted
on the very machine where the product runs, which is where trusting it
matters. The `test_gemini_thinking` module had the mirror-image problem: it
used `os.environ.setdefault`, which leaks past the test and made six
*other* modules fail depending on run order. It now scopes the variable to
each test.

**Full run with `GEMINI_API_KEY=test-key` set — the state of the live
server** — 538 tests, all passing:

| Modules | Tests |
|---|---|
| `test_gemini_thinking`, `test_configuration_integrity`, `test_llm_*`, `test_generation_trace` | 215 |
| `test_profile_defect_pass`, `test_silk_review_fixes`, `test_qa_enhancements_*`, `test_issue28_fixes`, `test_spec_contract` | 88 |
| `test_flows`, `test_backfill`, `test_clarifications`, `test_deal_assessment`, `test_gap_fixes` | 93 |
| `test_change_request_pass`, `test_exports`, `test_issue*`, `test_qa_*_fixes` | 86 |
| `tests.engines`, `tests.isolation`, `tests.investors` | 56 |

`makemigrations --check` reports no pending model changes.

---

## Deploying

```bash
python manage.py migrate                  # llm.0011 — clears the 2048 ceilings
python manage.py seed_platform_config     # repairs the tiers and capabilities
sudo systemctl restart fundos-web fundos-worker
python manage.py diagnose_config
```

`seed_platform_config` converges rather than overwrites: it acts only where
configuration is absent or provably broken. Expect it to report the tiers it
repaired and the ones it left alone.

Verified end to end on a fresh database seeded with only `GEMINI_API_KEY`
set and the endpoint hand-coded as `GEMINI LLM`, with the global `simple`
and `judgment` rows switched off to reproduce the reported failure:

```
llm endpoint: ignoring duplicate endpoint(s) GEMINI — another row already
              serves the same provider
llm provider selection: gemini via GEMINI LLM (simple=gemini-3.1-flash-lite,
                        judgment=gemini-3.1-pro)
llm provider selection: left as-is (advanced=gemini-3.6-flash)

simple    tier.simple.gemini     gemini-3.1-flash-lite  search=False
advanced  tier.advanced.gemini   gemini-3.6-flash       search=True
          max_uses=6  schema=False
judgment  tier.judgment.gemini   gemini-3.1-pro         thinking=tokens/4000
```

`diagnose_config` then reports PASS on all seven MODEL CHAIN checks and on
Output token ceilings.
