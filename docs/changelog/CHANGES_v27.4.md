# FundOS backend v27.4 — the contract reaches the wire, and the run states its cost

**Seven files changed, one added. No migrations. No API contract change.**

v27.3 introduced the tier contract and claimed it made ungrounded generation
*"unreachable rather than merely detectable."* A re-audit against the shipped
tarball found **four routes by which a search-required call still reached a
provider with no search tool**, each reproduced in a test before it was fixed.
The contract was correct. It was installed one layer below the code that
overrode it.

This release closes those routes and makes a run report its own economics, so
the next audit reads a log instead of reconstructing one.

---

## Why v27.3's flagship fix did not hold

`capabilities_payload()` applies the contract and its docstring says the tier
"has the last word." It had the last word **inside that function**. Four things
ran afterwards:

| # | Route | What happened |
|---|---|---|
| 1 | `except Exception` | `assert_role_tier_coherent` raised `TierContractViolation`; the adapter called it inside `try: … except Exception: config_profile = None`, which caught it, discarded it, and proceeded with `capabilities={}` |
| 2 | Cost control 3 | `research_synthesis` was in **both** `NO_THINKING_ROLES` and `SEARCH_REQUIRED_ROLES`; the contract set `budget_tokens=2048` and the adapter reset it to `0` four lines later |
| 3 | Unseeded profile | The contract only runs when a profile **row** resolves. With none seeded, `capabilities = {}` and it never executed at all |
| 4 | Search breaker | Stripped `web_search` from a search-required role and continued |

Route 2 is the one worth sitting with: it reproduced the exact v27.3 root cause
— `google_search` is model-decided, so a model with no deliberation budget has
no step in which to choose to call it — **inside the release that fixed it**.

A fifth defect was structural rather than behavioural. `SEARCH_EXPECTED_ROLES`
(adapter) and `SEARCH_REQUIRED_ROLES` (tier contract) were maintained by hand
and had already drifted three ways. `peer_insight` was required but not
expected, so it was forced onto a searching tier and then received neither the
`SEARCH FIRST` directive, nor the tail reminder, nor any warning when it
returned zero searches — the one role guaranteed to fail silently.

---

## What changed

### 1. The violation reaches the caller — `adapter.py`

Both `resolve_tier_profile_code` call sites now name `TierContractViolation`
before the broad handler and re-raise. A misconfigured tier is an admin fault
with a legible message and a fixable cause; failing the run is the correct
outcome, because the alternative is publishing recollection as research.

### 2. Optimisations do not override invariants — `adapter.py`

Cost control 3 now skips when the role is search-required **or** `web_search`
is attached to the call, and says so at INFO. Precedence is stated once, in
one place: a thinking budget that a **tool** depends on is an invariant;
saving output tokens is an optimisation.

`research_synthesis` is removed from `NO_THINKING_ROLES` — the one real
instance — and an **import-time check** logs loudly if the two sets ever
overlap again. Logged rather than raised: a hard failure at import takes the
service down, and the runtime guard already keeps behaviour correct.

### 3. One authority for the search role sets — `adapter.py`

```
SEARCH_EXPECTED_ROLES = SEARCH_REQUIRED_ROLES | SEARCH_BENEFICIAL_ROLES
```

`SEARCH_REQUIRED_ROLES` lives with the contract that enforces it.
`SEARCH_BENEFICIAL_ROLES` is the genuine second category: retrieval improves
the answer but its absence does not make the answer dishonest
(`market_research`, `company_profile_consolidated`). `EXPECTED` is derived and
is never edited directly.

### 4. Both truncation points are now visible — `services.py`, `adapter.py`

v27.3 raised `MAX_CONTEXT_CHARS` 24,000 → 120,000 and added a warning. That
was correct and it fixed the **second** cut. The first and larger one is
`MAX_WEBSITE_CHARS = 6000`, applied **per page** in `compress_payloads`,
before the adapter ever sees the payload — MediBuddy's `website_chars=31001`
was measured *after* it ran, so the 22% loss quantified in the v27.3 changelog
is what survived this function.

* `_clean_text` records every tail-slice and appends `[... N characters
  truncated — this page is incomplete]`, so a truncated page no longer reads
  as a short one and *"not stated on the website"* stops being a conclusion the
  model can draw silently.
* `compress_payloads` emits **one** `sources.compression` line per run —
  `chars_in`, `chars_kept`, `chars_dropped`, `pct_dropped`, the three worst
  keys, and the caps in force. WARN at ≥10% loss. One aggregate line, because
  a dozen per-key lines get skimmed exactly like the eighteen `cp_extract`
  warnings did.
* `_trim_bulk_sources` emits `llm.context.fanout_trim`, and only when it
  actually cuts, so a healthy run adds no lines.

### 5. `llmCalls` counts the whole run — `services.py`

v27.2 reported 9, the changelog claimed 7, the log showed 11. v27.3 counted
properly but opened the window inside `_generate_consolidated`, which excludes
`_run_assessment_inputs` — the **caller** invokes that after the function
returns. 10 of 11: less wrong, still wrong, and still not reconcilable against
a bill. The per-section path reported no count at all.

The window now opens at the top of `_generate_profile_inner` and closes after
assessment, on **both** paths. The only defensible definition of "calls this
run made" is "calls dispatched between the run starting and the run finishing."

### 6. Runs state their own economics — `fundos/llm/ledger.py` (new)

Costing the 17 Aug runs meant parsing eleven `llm.call.*` lines and adding six
columns by hand, and the total still came out 39% low. Every fact that analysis
produced is now a field:

```
run.economics  status="WARN" mode="consolidated" llm_calls=11
  prompt_tokens=46061 completion_tokens=8202 thinking_tokens=6266
  billed_output_tokens=14468 total_tokens=60529 cost_inr=17.54
  searches=0 searches_unknown=11 redundant_input_pct=44.4
  concerns="no web search was performed by any call"

run.economics.role  role="company_profile_records"  calls=4
  prompt_tokens=16452 completion_tokens=280 cost_inr=2.39
  tokens_in_per_token_out=58.8
```

That last figure is C-03's fingerprint. A role at 3:1 is doing work; a role at
**58.8:1** is re-reading a corpus to produce a paragraph, and it is the number
that should fall when the deep-extract schema is widened.

Design notes worth knowing:

* Entries are written in `_log_call`, the **single choke point** every dispatch
  path funnels through (success, fallback, mocked, hard failure), so the ledger
  and `LLMCallLog` cannot disagree about how many calls were made or what they
  cost — the price is computed once and shared.
* The ledger is written **before** and **outside** `_log_call`'s `try`. That
  function deliberately swallows its own exceptions so tracking can never break
  a generation, which means the database row is not guaranteed; a run whose DB
  writes failed is precisely when someone needs the accounting.
* `searches_unknown` is reported separately from `searches`. A provider
  returning no grounding metadata is **not** the same fact as a provider
  reporting zero searches, and the two need opposite fixes. Rendering them
  identically is what cost six releases.
* Thread-local. Celery runs concurrent generations in one process; a shared
  list would blend two tenants into a cost figure belonging to neither.

### 7. A refusal is not an outage — `core/exceptions.py`

New `UngroundableCall(LLMUnavailable)`. Found while fixing route 4: raising
plain `LLMUnavailable` would have been caught by the consolidated path's
generic handler and **fallen back to the per-section route, which does not
search either** — converting a refusal into a quieter version of the same
ungrounded profile. `_generate_profile_inner` now catches it before the
fallback and abandons the run with the cause on screen.

The breaker change is deliberately narrow. It still **degrades**
`market_research`, which is publishable without search; it now **refuses**
`company_profile_deep_extract`, which is not. The breaker may thin an answer;
it may not falsify one.

### 8. `CHANGES_v27.2.md` scenario table corrected (C-07)

Annotated in place rather than silently edited, with a pointer to re-derive it
from `run.economics`. A hand-written scenario table is what produced three
answers to one question.

---

## Test results

```
Ran 231 tests   LLM, generation, grounding, cost, v27.3, v27.4   OK (1 skipped)
Ran 205 tests   config, flows, exports, contract, defect passes  OK
Ran 143 tests   backfill, QA passes, benchmarks, assessment      OK
Ran  56 tests   engines, investors, isolation                    OK
--------------------------------------------------------------------------
      635 tests                                                  0 failures
```

`test_v23_phase3_phase4` and `test_v24_workbook_config` report 6 failures /
3 errors / 54 skipped — **identical on the untouched v27.3 tarball**, verified
by running both trees. They need a sector workbook fixture that is not in the
repo.

**Two test files added.** `test_v27_4_contract_and_economics.py` (32 tests) —
every test corresponds to a defect reproduced against v27.3 before its fix was
written, and the docstrings say which, because a test whose purpose is
forgotten is a test that gets deleted the next time it is inconvenient.
`test_v27_4_evidence_run.py` (2) — a harness that performs a full generation
and prints the diagnostics, plus a replay of run `332d9f` through
`run.economics` that reproduces the hand-computed 17 Aug totals to within
rounding.

**One existing test changed.**
`test_open_breaker_strips_web_search_from_the_call` asserted the old degrade
behaviour on `company_profile_deep_extract`. It now asserts refusal, and a
sibling test covers the unchanged degrade path on an optional role — so the
containment behaviour still has coverage where it was correct.

---

## What this release does NOT fix

**H1 — whether `responseSchema` suppresses `google_search`.** Still open, and
still the single highest-value unknown. `tools/diagnose_gemini_search.py`
settles it in eight calls. **See `RUNBOOK_v27_4_verification.md`** — it cannot
be answered without calling the live API, and it must be answered before the
next round of tuning or the same failure recurs against a different variable.

**C-03 — the fan-out.** Untouched, deliberately. The root fix extends the
deep-extract schema to cover `company_overview`, `cap_table` and
`market_research`, which means changing a prompt whose output cannot be
validated without the live model. `tokens_in_per_token_out` is now the
instrument for driving it.

**C-04 — explicit Gemini context caching.** Needs `cachedContent`; API work
rather than a fix.

**D-01 — nine fixture research adapters.** Unchanged and still the largest
single constraint on output quality.

**D-02 — `SectorMapping` has no 'Eyewear' row.** One admin row.

**X-01 — frontend v25.2 / backend skew.** No frontend tree was supplied with
either tarball, so this could not be checked at all. `_abandon_ungrounded_run`
now fires on two additional paths (§7 above), so confirming the client renders
an abandoned run as terminal rather than spinning has become *more* important,
not less.

---

## Verifying this release

```bash
python manage.py check
python manage.py test tests.api.test_v27_4_contract_and_economics
python manage.py test tests.api.test_v27_4_evidence_run -v 2   # prints the lines
python manage.py diagnose_config
```

Then follow `RUNBOOK_v27_4_verification.md` for the live run. Success on a real
generation is `searches` **greater than zero** on
`run.economics` — not the absence of a warning.
