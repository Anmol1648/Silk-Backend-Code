# v21 — configuration that survives a deploy, and rows that survive a run

Three themes, all from the two-company ingestion log and the configuration
dump alongside it: seeding must not overwrite an administrator, the
judgement tier must not truncate itself, and the AI's output must not be
discarded wholesale over a key name.

---

## 1. The seeder no longer overwrites configuration

**What happened.** `_repair_tier` treats a tier as broken when
`TenantLLMTier.resolve_code()` returns nothing — which an *inactive tier
row* does. It then rebuilt the profile from the `_RECOMMENDED` table,
overwriting `model_string`. So the only thing actually wrong was a boolean
flag, and repairing it destroyed a deliberately-chosen model, replacing it
with `gemini-3.1-pro` — a name the API does not serve. Every judgement call
on the live install 404'd as a result.

The rule now: **fill what is empty, fix what is provably broken, touch
nothing else.**

* An inactive tier row is **reactivated first**, and the profile it points
  at is then re-examined. A flag problem is fixed as a flag problem.
* A profile that is merely switched off is reactivated rather than rebuilt.
* `model_string` is replaced **only** when it is blank, or when it names
  something absent from the catalog — where the call would fail outright, so
  leaving it is not a kindness. A catalogued model an administrator chose is
  never "corrected".
* The judgement thinking budget is applied only to a **newly created**
  profile. `none` on an existing one is a choice — and on Gemini often the
  right one — so a seed run does not switch reasoning back on behind
  somebody who turned it off.
* Every change to configuration that already existed is journalled and
  printed together at the end as `field: old → new`, with the reason.
* New `--dry-run`: reports what a seed run *would* change to existing
  configuration, then rolls back without writing.

Seeding happens at deploy time, when nobody is watching the output. An
overwrite has to be visible at the moment it happens, not discovered three
generations later in a trace as a model name the API refuses.

**Seeded Gemini model names corrected** to `gemini-3.5-flash` across all
three tiers and `_RECOMMENDED`. The previous values (`gemini-3.1-pro`,
`gemini-3.1-flash-lite`, `gemini-3.6-flash`) were asserted, never verified;
only one of the three is served by a standard key. `_RECOMMENDED` is now
explicitly a fallback for unset values, and deliberately conservative — the
right default is the model most likely to actually answer, not the newest
name.

---

## 2. The judgement tier truncated itself on every run

```
model=gemini-3.5-flash  thinking_tokens=3932  completion_tokens=160
finish_reason="MAX_TOKENS"  was_repaired=true
```

The judgement thinking budget is 4000; `ROLE_MAX_OUTPUT_TOKENS` for that
role is 4096. The headroom guard read `if max_tokens <= thinking_budget` —
4096 is greater than 4000, so it never fired, leaving **96 tokens to answer
in**. The model spent 3,932 thinking and was cut off mid-JSON. Every
adjudication in the log is a repaired fragment.

`maxOutputTokens` is now always `thinking_budget + the answer allowance`,
never merely larger than the budget.

---

## 3. Form seeding discarded real content on every run

`revenue_model` reported *Invalid row* (rows arriving as strings, not
objects) and `company_metrics` *This field is required* (objects whose label
sat under a different key). Between 1 and 10 rows were lost per section per
run. `_records_items_to_api()` was a pass-through that normalised nothing,
and one bad row failed the whole batch.

The validator is strict by design — it backs a founder-facing form where a
wrong number is worse than a missing one. But the AI is not filling in that
form; it is proposing rows. So the normalisation belongs between them:

* **Key aliases per form** — `name`/`stream`/`source`/`segment` → `label`,
  `share`/`percentage`/`contribution` → `pct`, `metric`/`kpi` → `label`,
  and so on.
* **Numbers read as written** — `"45%"`, `"1,250"`, `"12.5 %"`, and Indian
  magnitudes (`crore`, `lakh`, `mn`, `bn`), which appear constantly in this
  market.
* **Bare strings salvaged** — `"Retail 45%"` becomes
  `{label: "Retail", pct: 45.0}` rather than being discarded.
* **Per-row tolerance** — an unusable row is dropped with its reason in the
  trace; the rest are kept.
* **Percentages rescaled, not rejected** — a split the model rounded to 99
  is normalised to 100, since the validator's ±0.5 tolerance is not worth
  losing four rows over.

A FAIL now carries a sample of the offending row, so the log names what
arrived instead of only what was wanted.

---

## 4. Unresolvable config-profile codes bypassed the whole tier system

The section, record and structured calls request
`cp_extract.<section_key>`. **No such profile has ever been seeded.**
`_resolve_config_profile()` returned `None`, and because `config_profile`
was non-`None` on the way in, `llm_generate` skipped tier resolution
entirely — so those calls ran with no capabilities, no search, no thinking
control, and the endpoint's default model.

That is most of the calls in every run, and it means tier settings had
almost no effect on them. This is why the live logs showed `gemini-3.5-flash`
everywhere while the simple tier said `gemini-3.1-flash-lite`.

An unresolvable code now **falls back to the tier**, logs a warning, and
emits `llm.config_profile_missing` into the trace naming what was requested
and what it fell back to.

---

## 5. `searches` was empty on every run — because nothing asked

Gemini's search grounding is model-initiated: the tool is offered, the model
decides. The deep-extract prompt opened *"Use ONLY the supplied context"*
and *"never invent"*, which gives it no reason to ever search. The tool was
available and declined on all thirteen logged runs.

The prompt now opens with a RESEARCH FIRST block: use the search tool if it
is available, and search specifically for funding rounds and investors,
filings, audited revenue and profit by year, named leadership, headcount and
material news; prefer primary sources. Critically, it also says searching
widens what counts as a source and **does not license recalling figures from
memory** — the failure mode search is meant to remove, not introduce.

---

## 6. Website adapter — patterns adopted from the supplied scraper

* **HTML tables extracted with their structure intact.** Indian company
  sites publish revenue, capacity and shareholding as tables; flattening
  them into prose and asking the model to re-parse loses the row/column
  relationship that made them facts. Rows are emitted as `A | B | C` under a
  `[TABLE]` marker, with merged `colspan` footer rows dropped and
  single-row layout tables ignored — the same discipline as
  `_scrape_deal_data`.
* **Per-host circuit breaker.** Three consecutive failures opens the
  circuit for five minutes. Ingesting many companies otherwise means one
  dead or blocking host burns its full retry budget on every run.
* **Transient-only retries.** Timeouts, connection resets and chunked-
  encoding failures retry with a delay; a 4xx is a considered answer from
  the server and is not repeated.
* `client_rendered` (v20) remains the flag a headless renderer should key
  off, so the expensive path would run only for sites that need it. The
  renderer itself is **not** in this build — see below.

---

## 7. Concurrent generations no longer corrupt a profile

Two runs started ten seconds apart against the same company. Nothing
serialised them: the "only seed when the section is empty" checks each saw
an empty section and both wrote. Worse was observed —

```
IntegrityError: Key (profile_id)=(…) is not present in company_profile
```

— five section writes failing because the profile row was replaced beneath a
run holding a stale object.

`generate_profile` now takes a **PostgreSQL advisory lock** per profile: no
schema, no cost, released automatically if the process dies, and it does not
block reads while a run is in progress. A second run is refused with
`GenerationAlreadyRunning` rather than queued, since queueing would just
duplicate work already underway. Under the lock the profile row is re-read
and its existence confirmed. On backends without advisory locks (sqlite in
tests) this degrades to a no-op, which is correct — they are single-writer.

---

## Tests

`tests/api/test_v21_accuracy.py` — 17 new tests, including a replay of the
exact sequence that destroyed the live configuration (admin sets a model,
tier row switched off, seed run: row reactivated, model preserved).

Full run with `GEMINI_API_KEY` set — the state of the live server —
**569 tests, all passing**: LLM and grounding 246, profile and QA 88, flows
93, issues and exports 86, engines/isolation/investors 56.
`makemigrations --check` reports no pending model changes.

---

## Deploying

```bash
python manage.py seed_platform_config --dry-run   # read this output first
python manage.py migrate
python manage.py seed_platform_config
sudo systemctl restart fundos-web fundos-worker
python manage.py diagnose_config
```

No new migrations in v21; `migrate` is there to confirm v20's are applied.

Then regenerate one company and read four lines:

* `llm.call.company_profile_judgment` — `finish_reason=STOP`, not
  `MAX_TOKENS`, and `was_repaired=false`.
* `records.form_seed` — absent, or WARN with a `dropped` count rather than
  FAIL.
* `llm.call.company_profile_deep_extract` — `searches=` non-empty.
* `llm.config_profile_missing` — WARN naming `cp_extract.<section>`. Expect
  this; it is the diagnostic for §4, not a regression.

---

## Deliberately NOT in this build

**The headless renderer.** `pyppeteer` plus a Chrome binary is a deployment
dependency, not a code change — it needs a Chrome install, a memory budget
on the worker, and a decision about per-render timeouts. The trigger
(`client_rendered`) and the circuit breaker that would protect it are both
in place. ril.com's 6,147 characters is the JS ceiling until this lands.

**The Inc42 funding adapter.** `PublicFundingAdapter` is still a fixture.
Wiring the supplied scraper's data in would populate funding history from a
real source for the Indian market — the single biggest accuracy gain
available — but it needs decisions on refresh cadence and on matching
company names against deal records. It is a data pipeline, not a bug fix.

**LinkedIn.** Scraping it breaches their terms and is actively pursued;
the risk is account bans, IP blocks and legal exposure. The adapter stays
behind its existing `legal_cleared` gate. The defensible routes are founders
supplying their own profile data at onboarding, or a licensed people-data
provider. That is a decision for whoever owns legal risk, not an
engineering choice.

**Per-field provenance.** Still no source URL or retrieval date on an
individual extracted figure. The scraper's `source_url`-per-row is the right
model. It needs a schema change and a view on how verified and unverified
fields should render.
